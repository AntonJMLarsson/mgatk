import click
import os
import yaml
import os.path
import sys
import shutil
import random
import string
import itertools
import time
import pysam
from pkg_resources import get_distribution
from subprocess import call, check_call
from .mgatkHelp import *

# ------------------------------
# Command line arguments
# ------------------------------	

@click.command()
@click.version_option()
@click.argument('mode', type=click.Choice(['call', 'check']))
@click.option('--input', '-i',  default = ".", required=True, help='input directory; assumes .bam / .bam.bai files are present')
@click.option('--output', '-o', default="mgatk_out", required=True, help='Output directory for analysis')
@click.option('--name', '-n', default="mgatk", required=True, help='Prefix for project name')

@click.option('--mito-genome', '-m', default = "hg19", required=True, help='mitochondrial genome configuration. Choose hg19, mm10, or a custom .fasta file (see documentation)')
@click.option('--ncores', '-c', default = "detect", required=True, help='Number of cores to run job in parallel.')
@click.option('--bams-ready', '-r', is_flag=True, help='Input bam files are already filtered for only the mitochondrial genome and sorted; can still remove duplicates optionally though; this flag also bypasses clipping')

@click.option('--filter-flags', '-ff', is_flag=True, help='Manual specification of .sam headers to filter for; see documentation.')
@click.option('--atac-single', '-as', is_flag=True, help='Default parameters for ATAC-Seq single end read analyses; see documentation.')
@click.option('--atac-paired', '-ap',  is_flag=True, help='Default parameters for ATAC-Seq paired end read analyses; see documentation.')
@click.option('--rna-single', '-rs', is_flag=True, help='Default parameters for RNA-Seq single end read analyses; see documentation.')
@click.option('--rna-paired', '-rp', is_flag=True, help='Default parameters for RNA-Seq paired end read analyses; see documentation.')

@click.option('--keep-duplicates', '-kd', is_flag=True, help='Keep marked (presumably PCR) duplicates; recommended for low-coverage RNA-Seq')
@click.option('--keep-indels', '-ki', is_flag=True, help='Keep marked indels for analysis; not recommended as this flag has not been well-tested')
@click.option('--read-qual', '-q', default = "20", required=True, help='Minimum read quality for final filter.')

@click.option('--clipL', '-cl', default = "0", required=True, help='Number of variants to clip from left hand side of read.')
@click.option('--clipR', '-cr', default = "0", required=True, help='Number of variants to clip from right hand side of read.')

@click.option('--keep-samples', '-k', default="ALL", help='Comma separated list of sample names to keep; ALL (special string) by default. Sample refers to basename of .bam file')
@click.option('--ignore-samples', '-g', default="NONE", help='Comma separated list of sample names to ignore; NONE (special string) by default. Sample refers to basename of .bam file')

@click.option('--keep-temp-files', '-t', is_flag=True, help='Keep all intermediate files.')
@click.option('--skip-rds', '-s', is_flag=True, help='Generate plain-text only output. Otherwise, this generates a .rds obejct that can be immediately read into R')

def main(mode, input, output, name, mito_genome, ncores, bams_ready, filter_flags,
	atac_single, atac_paired, rna_single, rna_paired, keep_duplicates, keep_indels,
	read_qual, clipl, clipr, keep_samples, ignore_samples, keep_temp_files, skip_rds):
	
	"""mgatk: a mitochondrial genome analysis toolkit."""
	__version__ = get_distribution('mgatk').version
	script_dir = os.path.dirname(os.path.realpath(__file__))

	click.echo(gettime() + "mgatk v%s" % __version__)
	if(mode == "check"):
		click.echo(gettime() + "checking dependencies...")

	# -------------------------------
	# Synonyms
	# -------------------------------
	# When variable names get updated
	filtered_sorted = bams_ready

	# -------------------------------
	# Verify dependencies
	# -------------------------------
	
	check_software_exists("R")
	check_software_exists("bcftools")
	check_software_exists("tabix")
	check_software_exists("python")
	check_software_exists("samtools")
	check_software_exists("java")
	check_R_packages(['mgatk', 'ggplot2'])
	
	# -------------------------------
	# Determine samples for analysis
	# -------------------------------

	bams = os.popen('ls ' + input + '/*.bam').read().strip().split("\n")
	samples = []
	samplebams = []
	
	find = re.compile(r"^[^.]*")

	for bam in bams:
		#if(os.path.isfile(bam + ".bai")):
		samples.append(re.search(find, os.path.basename(bam)).group(0))
		samplebams.append(bam)
	
	if(keep_samples != "ALL"):
		keeplist = keep_samples.split(",")
		click.echo(gettime() + "Intersecting detected samples with user-retained ones: " + keep_samples)
		keepidx = findIdx(samples, keeplist)
		samples = [samples[i] for i in keepidx]
		samplebams = [samplebams[i] for i in keepidx]
		
	if(ignore_samples != "NONE"):
		iglist = ignore_samples.split(",")
		click.echo(gettime() + "Attempting to remove samples from processing:" + ignore_samples)
		rmidx = findIdx(samples, iglist)
		for index in sorted(rmidx, reverse=True):
			del samples[index]
			del samplebams[index]
    		
	if not len(samples) > 0:
		sys.exit('ERROR: Could not import any samples from the user specification; check flags, logs and input configuration; QUITTING')

	if(mode == "check"):
		sys.exit(gettime() + "mgatk check passed! The software will process " + str(len(samples)) + " samples if same parameters are run in `call` mode")
	
	# -------------------------------
	# Setup output folder
	# -------------------------------
	
	outfolder = output
	logfolder = outfolder + "/logs"
	fastafolder = outfolder + "/fasta"
	internfolder = outfolder + "/.internal"
	parselfolder = internfolder + "/parseltongue"
	samplesfolder = internfolder + "/samples"
	
	# Check if output directories exist; make if not
	if not os.path.exists(outfolder):
		os.makedirs(outfolder)
		os.makedirs(outfolder + "/final")
	if not os.path.exists(logfolder):
		os.makedirs(logfolder)
		if not(keep_duplicates):
			os.makedirs(logfolder + "/rmdupslogs")
	if not os.path.exists(fastafolder):
		os.makedirs(fastafolder)	
	if not os.path.exists(internfolder):
		os.makedirs(internfolder)
		with open(internfolder + "/README" , 'w') as outfile:
			outfile.write("This folder creates important (small) intermediate; don't modify it.\n\n")	
	if not os.path.exists(parselfolder):
		os.makedirs(parselfolder)
		with open(parselfolder + "/README" , 'w') as outfile:
			outfile.write("This folder creates intermediate output to be interpreted by Snakemake; don't modify it.\n\n")
	if not os.path.exists(samplesfolder):
		os.makedirs(samplesfolder)
		with open(samplesfolder + "/README" , 'w') as outfile:
			outfile.write("This folder creates samples to be interpreted by Snakemake; don't modify it.\n\n")
	
	# Set up sample bam plain text file
	for i in range(len(samples)):
		with open(samplesfolder + "/" + samples[i] + ".bam.txt" , 'w') as outfile:
			outfile.write(samplebams[i])
				
	cwd = os.getcwd()
	logf = open(logfolder + "/base.mgatk.log", 'a')
	
	
	# -----------------------------------
	# Parse user-specified parameteres
	# -----------------------------------
	
	####################
	# Handle .fasta file
	####################
	supported_genomes = ['hg19', 'mm10']
	if any(mito_genome in s for s in supported_genomes):
		click.echo(gettime() + "Found designated mitochondrial genome: %s" % mito_genome, logf)
		fastaf = script_dir + "/bin/anno/fasta/" + mito_genome + "_mtDNA.fasta"
	else:
		if os.path.exists(mito_genome):
			fastaf = mito_genome
		else:
			sys.exit('ERROR: Could not find file ' + mito_genome + '; QUITTING')
	fasta = parse_fasta(fastaf)	

	if(len(fasta.keys()) != 1):
		sys.exit('ERROR: .fasta file has multiple chromosomes; supply file with only 1; QUITTING')
	mito_genome, mito_seq = list(fasta.items())[0]
	mito_length = len(mito_seq)
	
	shutil.copyfile(fastaf, fastafolder + "/" + mito_genome + ".fasta")
	fastaf = fastafolder + "/" + mito_genome + ".fasta"
	pysam.faidx(fastaf)
	
	f = open(outfolder + "/final/" + name + "." + mito_genome + "_refAllele.txt", 'w')
	b = 1
	for base in mito_seq:
		f.write(str(b) + "\t" + base + "\n")
		b += 1
	f.close()
		
	##############################
	# Other command line arguments
	##############################
	
	if(keep_indels):
		skip_indels = ""
	else:
		skip_indels = "--skip-indels "
		
	if(ncores == "detect"):
		ncores = str(available_cpu_count())
	else:
		ncores = str(ncores)
		
	click.echo(gettime() + "Processing .bams with "+ncores+" cores", logf)
	click.echo(gettime() + "Processing .bams with "+ncores+" cores")
	
	# -------------------
	# Process each sample
	# -------------------
	tempfolder = outfolder + "/temp"
	if not os.path.exists(tempfolder):
		os.makedirs(tempfolder)
		os.makedirs(tempfolder + "/temp_bam")
		os.makedirs(tempfolder + "/ready_bam")
		os.makedirs(tempfolder + "/vcf")
	
	qcfolder = outfolder + "/qc"
	if not os.path.exists(qcfolder):
		os.makedirs(qcfolder)
		os.makedirs(qcfolder + "/BAQ")
		os.makedirs(qcfolder + "/BQ")
		os.makedirs(qcfolder + "/depth")
					
	snakedict1 = {'input_directory' : input, 'output_directory' : output, 'script_dir' : script_dir,
		'fasta_file' : fastaf, 'mito_genome' : mito_genome, 'mito_length' : mito_length, 'name' : name,
		'read_qual' : read_qual, 'filtered_sorted' : filtered_sorted, 'keep_duplicates' : keep_duplicates,
		'skip_indels' : skip_indels, 'clipl' : clipl, 'clipr' : clipr}
	
	y1 = parselfolder + "/snake.scatter.yaml"
	with open(y1, 'w') as yaml_file:
		yaml.dump(snakedict1, yaml_file, default_flow_style=False)
	
	# For making the DAG
	#dagcall = 'snakemake --snakefile ' + script_dir + '/bin/snake/Snakefile.Scatter --cores '+ncores+' --config cfp="' + y1 + '" --rulegraph -T'
	#os.system(dagcall)
	
	snakefile1 = 'snakemake --snakefile ' + script_dir + '/bin/snake/Snakefile.Scatter --cores '+ncores+' --config cfp="' + y1 + '" -T'
	os.system(snakefile1)
	click.echo(gettime() + "mgatk successfully processed the supplied .bam files", logf)
	
	if keep_temp_files:
		click.echo(gettime() + "Temporary files not deleted since --keep-temp-files was specified.", logf)
	else:
		shutil.rmtree(fastafolder)
		shutil.rmtree(internfolder)
		shutil.rmtree(tempfolder)
		click.echo(gettime() + "Intermediate files successfully removed.", logf)
		
	# Suspend logging
	logf.close()
	
	
