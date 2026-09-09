# CompBioBench workspace

You are answering one CompBioBench question in this directory. Bioinformatics
CLIs from the cloned `compbio-benchmark` conda env are already on `PATH`
(samtools, bowtie2, STAR, macs2, idr, picard, cutadapt, chromap, kraken2,
caper, java, pigz, …). Do not `conda activate`.

## Local cache

A read-only cache is mounted at `local_cache/` (same tree as
`$COMPBIO_CACHE_DIR`). Prefer it over re-downloading genomes, aligner indexes,
containers, and models. See `local_cache/INDEX.md`. Hugging Face and
Singularity cache environment variables already point there.

## Kraken2

If `local_cache/kraken2/db/hash.k2d` exists, classify with
`kraken2 --db local_cache/kraken2/db`. That is a host-provided database
already on this machine. Do **not** `axel`/`wget` `k2_pluspf_*.tar.gz` or
other genome-idx Kraken tarballs.

## Caper / Cromwell

Timeouts on ENCODE ATAC (`encode-atac-pipeline-q1`) were Caper/Cromwell
**launch** failures (HSQLDB init, no workflow UUID, no `cromwell-executions/`),
not alignment runtime. Successful runs finished in about 25–30 minutes once
Cromwell started. Do not sit on a hung launch until the question timeout.

- Use a unique Cromwell/HSQLDB file and output directory in this workspace.
- Persist Caper and Cromwell stderr to files here; a killed run otherwise
  leaves no error log.
- If there is no workflow UUID and no `cromwell-executions/` within 5 minutes,
  abort that launch and retry with a fresh DB.
- Keep the pipeline timeout several minutes below the question limit so you
  can extract `qc.json` and emit the final answer.
- Do not treat leftover `atac_out/` or `qc.json` as success unless this
  attempt produced them.

## Output

The harness grades by exact string match. Return exactly one line containing
only the final answer.
