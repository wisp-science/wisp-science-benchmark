#!/usr/bin/env python3
"""Pre-install CompBioBench runtime assets and prefer a local cache.

Warmup fills a shared cache so question timeouts are spent on analysis, not on
pulling Docker images, building conda envs, or downloading hg38 / model weights.

    python run_benchmark.py warmup
    python warmup.py --status
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable
from urllib.parse import unquote, urlparse


CACHE_LINK_NAME = "local_cache"
BASE_ENV_NAME = "compbio-benchmark"
HERE = Path(__file__).resolve().parent
ENV_FILE = HERE / "environment.yml"
EXTRAS_FILE = HERE / "environment-extras.yml"
USER_AGENT = "compbiobench-warmup/1"

# ENCODE ATAC v2.2.3 genome TSV v4 (same files as genome_tsv/v4/hg38.tsv).
ENCODE_HG38_TSV: list[tuple[str, str]] = [
    (
        "ref_fa",
        "https://www.encodeproject.org/files/GRCh38_no_alt_analysis_set_GCA_000001405.15/"
        "@@download/GRCh38_no_alt_analysis_set_GCA_000001405.15.fasta.gz",
    ),
    (
        "ref_mito_fa",
        "https://www.encodeproject.org/files/GRCh38_no_alt_analysis_set_GCA_000001405.15_mito_only/"
        "@@download/GRCh38_no_alt_analysis_set_GCA_000001405.15_mito_only.fasta.gz",
    ),
    (
        "blacklist",
        "https://www.encodeproject.org/files/ENCFF356LFX/@@download/ENCFF356LFX.bed.gz",
    ),
    (
        "chrsz",
        "https://www.encodeproject.org/files/GRCh38_EBV.chrom.sizes/"
        "@@download/GRCh38_EBV.chrom.sizes.tsv",
    ),
    (
        "bowtie2_idx_tar",
        "https://www.encodeproject.org/files/ENCFF110MCL/@@download/ENCFF110MCL.tar.gz",
    ),
    (
        "bowtie2_mito_idx_tar",
        "https://www.encodeproject.org/files/GRCh38_no_alt_analysis_set_GCA_000001405.15_mito_only_bowtie2_index/"
        "@@download/GRCh38_no_alt_analysis_set_GCA_000001405.15_mito_only_bowtie2_index.tar.gz",
    ),
    (
        "bwa_idx_tar",
        "https://www.encodeproject.org/files/ENCFF643CGH/@@download/ENCFF643CGH.tar.gz",
    ),
    (
        "bwa_mito_idx_tar",
        "https://www.encodeproject.org/files/GRCh38_no_alt_analysis_set_GCA_000001405.15_mito_only_bwa_index/"
        "@@download/GRCh38_no_alt_analysis_set_GCA_000001405.15_mito_only_bwa_index.tar.gz",
    ),
    ("tss", "https://www.encodeproject.org/files/ENCFF766FGL/@@download/ENCFF766FGL.bed.gz"),
    ("dnase", "https://www.encodeproject.org/files/ENCFF304XEX/@@download/ENCFF304XEX.bed.gz"),
    ("prom", "https://www.encodeproject.org/files/ENCFF140XLU/@@download/ENCFF140XLU.bed.gz"),
    ("enh", "https://www.encodeproject.org/files/ENCFF212UAV/@@download/ENCFF212UAV.bed.gz"),
    (
        "reg2map",
        "https://storage.googleapis.com/encode-pipeline-genome-data/hg38/ataqc/"
        "hg38_dnase_avg_fseq_signal_formatted.txt.gz",
    ),
    (
        "reg2map_bed",
        "https://storage.googleapis.com/encode-pipeline-genome-data/hg38/ataqc/"
        "hg38_celltype_compare_subsample.bed.gz",
    ),
    (
        "roadmap_meta",
        "https://storage.googleapis.com/encode-pipeline-genome-data/hg38/ataqc/"
        "hg38_dnase_avg_fseq_signal_metadata.txt",
    ),
]

DOCKER_IMAGES = ("encodedcc/atac-seq-pipeline:v2.2.3",)
# Official S3 SIF often 404s; docker image is the fallback source.
SINGULARITY_IMAGES = (
    (
        "atac-seq-pipeline_v2.2.3.sif",
        "https://encode-pipeline-singularity-image.s3.us-west-2.amazonaws.com/"
        "atac-seq-pipeline_v2.2.3.sif",
        "encodedcc/atac-seq-pipeline:v2.2.3",
    ),
)
HF_MODELS = (
    "kuleshov-group/caduceus-ph_seqlen-131k_d_model-256_n_layer-16",
    "johahi/borzoi-replicate-0",
)
# v0.0.5 checkpoints from GitHub (same SHA256 as JHU FTP; 2,878,124 bytes each).
# Source: splice-pred-q1 gpt-5.6-sol trace. JHU FTP is a fallback and often 502s.
OPENSPLICEAI_PT_SIZE = 2_878_124
OPENSPLICEAI_MODELS = tuple(
    (
        f"model_10000nt_rs{seed}.pt",
        (
            "https://raw.githubusercontent.com/Kuanhao-Chao/OpenSpliceAI/v0.0.5/"
            f"models/openspliceai-mane/10000nt/model_10000nt_rs{seed}.pt",
            f"https://ftp.ccb.jhu.edu/pub/data/OpenSpliceAI/OSAI-MANE/10000nt/"
            f"model_10000nt_rs{seed}.pt",
            f"http://ftp.ccb.jhu.edu/pub/data/OpenSpliceAI/OSAI-MANE/10000nt/"
            f"model_10000nt_rs{seed}.pt",
        ),
    )
    for seed in (10, 11, 12, 13, 14)
)
EBI_FILES = (
    (
        "gencode.vM31.annotation.gtf.gz",
        "https://ftp.ebi.ac.uk/pub/databases/gencode/Gencode_mouse/release_M31/"
        "gencode.vM31.annotation.gtf.gz",
    ),
)
KRAKEN_STANDARD_8GB = (
    "https://genome-idx.s3.amazonaws.com/kraken/k2_standard_08gb_20240605.tar.gz"
)
# Conda extras last: a full env update can stall for hours. Docker/genomes
# are what encode-atac-pipeline-q1 and find-deletion-q1 actually block on.
STEPS = ("network", "docker", "singularity", "genomes", "models", "conda", "caper")
CONDA_EXTRA_PACKAGES = (
    ("bowtie2", "bowtie2"),
    ("macs2", "macs2"),
    ("picard", "picard"),
    ("cutadapt", "cutadapt"),
    ("chromap", "chromap"),
    ("kraken2", "kraken2"),
    ("star", "STAR"),
    ("openjdk", "java"),
    ("wget", "wget"),
    ("pigz", "pigz"),
)
CONDA_EXTRA_PIP = ("caper", "huggingface_hub")
# bioconda idr builds stop at Python 3.10; PyPI "idr" is a different project.
# setup.py imports numpy at import time, so pip's isolated build env fails.
# Bundled inv_cdf.c was Cython'd for Python <=3.10; rebuild from .pyx.
IDR_PIP_SPEC = "https://github.com/kundajelab/idr/archive/refs/tags/2.0.4.2.tar.gz"
IDR_CYTHON_SPEC = "Cython>=0.29.32,<3"
IDR_PIP_ARGS = ("--no-build-isolation", "--no-deps", IDR_PIP_SPEC)
CONDA_PACKAGE_TIMEOUT = 900
CAPER_SMOKE_TIMEOUT = 300
CAPER_SMOKE_DIR = "caper-smoke"
HELLO_WDL = """version 1.0
workflow hello_probe {
  call echo
}
task echo {
  command { echo ok }
  output { String out = read_string(stdout()) }
}
"""
DEFAULT_DOCKER_MIRRORS = ("docker.m.daocloud.io",)


@dataclass(frozen=True)
class Probe:
    family: str
    name: str
    url: str
    required: bool = True


@dataclass
class ProbeResult:
    family: str
    name: str
    url: str
    required: bool
    ok: bool
    status: int | None
    elapsed_ms: int
    error: str | None


def huggingface_endpoint() -> str:
    return os.environ.get("HF_ENDPOINT", "https://huggingface.co").rstrip("/")


def network_probes() -> list[Probe]:
    hf = huggingface_endpoint()
    return [
        Probe("ENCODE", "homepage", "https://www.encodeproject.org/", required=False),
        Probe(
            "ENCODE",
            "search-api",
            "https://www.encodeproject.org/search/?type=Experiment&format=json&limit=0",
        ),
        Probe("Hugging Face", "hub", f"{hf}/"),
        Probe("Hugging Face", "hf-mirror", "https://hf-mirror.com/", required=False),
        Probe("NCBI", "homepage", "https://www.ncbi.nlm.nih.gov/"),
        Probe(
            "NCBI",
            "eutils",
            "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/einfo.fcgi",
            required=False,
        ),
        Probe("GEO", "homepage", "https://www.ncbi.nlm.nih.gov/geo/"),
        Probe("EBI", "homepage", "https://www.ebi.ac.uk/"),
        Probe(
            "EBI",
            "gencode-ftp",
            "https://ftp.ebi.ac.uk/pub/databases/gencode/Gencode_human/",
            required=False,
        ),
    ]


def default_cache_dir() -> Path:
    raw = os.environ.get("COMPBIO_CACHE_DIR", "").strip()
    if raw:
        return Path(raw).expanduser().resolve()
    data = os.environ.get("COMPBIO_DATA_DIR", "").strip()
    if data:
        return Path(data).expanduser().resolve().parent / "compbiobench-cache"
    return (Path.home() / "benchmark" / "compbiobench-cache").resolve()


def cache_environ(cache_dir: Path | None = None) -> dict[str, str]:
    """Env vars that make conda/HF/Singularity prefer the local cache."""
    cache_dir = cache_dir or default_cache_dir()
    hf = cache_dir / "models" / "huggingface"
    sif = cache_dir / "singularity"
    ncbi = cache_dir / "ncbi"
    env = {
        "COMPBIO_CACHE_DIR": str(cache_dir),
        "HF_HOME": str(hf),
        "HF_HUB_CACHE": str(hf / "hub"),
        "TRANSFORMERS_CACHE": str(hf / "transformers"),
        "HUGGINGFACE_HUB_CACHE": str(hf / "hub"),
        "SINGULARITY_CACHEDIR": str(sif),
        "APPTAINER_CACHEDIR": str(sif),
        "SINGULARITY_PULLFOLDER": str(sif),
        "APPTAINER_PULLFOLDER": str(sif),
        "NCBI_CACHE": str(ncbi),
    }
    endpoint = os.environ.get("HF_ENDPOINT", "").strip()
    if endpoint:
        env["HF_ENDPOINT"] = endpoint
    return env


def apply_cache_environ(cache_dir: Path | None = None) -> Path:
    cache_dir = cache_dir or default_cache_dir()
    for key, value in cache_environ(cache_dir).items():
        os.environ[key] = value
    return cache_dir


def url_filename(url: str) -> str:
    path = unquote(urlparse(url).path)
    name = Path(path).name
    return name or "download.bin"


def probe_url(url: str, timeout: float = 10.0) -> tuple[bool, int | None, str | None]:
    request = urllib.request.Request(
        url, method="GET", headers={"User-Agent": USER_AGENT}
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            response.read(256)
            status = getattr(response, "status", None) or response.getcode()
            return True, int(status) if status else 200, None
    except urllib.error.HTTPError as exc:
        return False, int(exc.code), str(exc.reason)
    except Exception as exc:
        return False, None, str(exc)


def network_precheck(timeout: float = 10.0) -> list[ProbeResult]:
    results: list[ProbeResult] = []
    for probe in network_probes():
        started = time.perf_counter()
        ok, status, error = probe_url(probe.url, timeout=timeout)
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        results.append(
            ProbeResult(
                family=probe.family,
                name=probe.name,
                url=probe.url,
                required=probe.required,
                ok=ok,
                status=status,
                elapsed_ms=elapsed_ms,
                error=error,
            )
        )
    return results


def required_network_failed(results: Iterable[ProbeResult]) -> list[ProbeResult]:
    return [item for item in results if item.required and not item.ok]


def maybe_enable_hf_mirror(results: Iterable[ProbeResult]) -> bool:
    """If the official hub is down and hf-mirror.com works, use the mirror."""
    items = list(results)
    hub = next((item for item in items if item.name == "hub"), None)
    mirror = next((item for item in items if item.name == "hf-mirror"), None)
    if os.environ.get("HF_ENDPOINT", "").strip():
        return False
    if hub is None or hub.ok or mirror is None or not mirror.ok:
        return False
    os.environ["HF_ENDPOINT"] = "https://hf-mirror.com"
    return True


def is_complete_file(path: Path) -> bool:
    return path.is_file() and path.stat().st_size > 0


def download_url(
    url: str,
    dest: Path,
    *,
    force: bool = False,
    timeout: int = 120,
) -> str:
    """Download url to dest, resuming when curl/wget is available. Returns status."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    if is_complete_file(dest) and not force:
        return "cached"
    tmp = dest.with_suffix(dest.suffix + ".part")
    curl = shutil.which("curl")
    wget = shutil.which("wget")
    if curl:
        cmd = [
            curl, "-fL", "--retry", "3", "--retry-delay", "2",
            "-A", USER_AGENT, "--connect-timeout", str(min(timeout, 30)),
            "--max-time", "0", "-C", "-", "-o", str(tmp), url,
        ]
    elif wget:
        cmd = [
            wget, "-c", "-O", str(tmp), f"--timeout={min(timeout, 30)}",
            f"--user-agent={USER_AGENT}", url,
        ]
    else:
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(request, timeout=timeout) as response, tmp.open("wb") as fh:
            shutil.copyfileobj(response, fh)
        tmp.replace(dest)
        return "downloaded"
    result = subprocess.run(cmd, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"download failed ({result.returncode}): {url}")
    tmp.replace(dest)
    return "downloaded"


def extract_tar(archive: Path, dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    marker = dest / ".extracted"
    if marker.exists():
        return
    subprocess.run(
        ["tar", "-xf", str(archive), "-C", str(dest)],
        check=True,
        capture_output=True,
        text=True,
    )
    marker.write_text(f"{archive.name}\n")


def conda_cmd() -> list[str]:
    import conda_cli
    return [conda_cli.solver_cli()]


def conda_env_prefix(name: str) -> Path | None:
    import conda_cli
    prefix = conda_cli.env_prefix(name)
    return Path(prefix) if prefix else None


def conda_env_exists(name: str) -> bool:
    import conda_cli
    return conda_cli.env_exists(name)


def conda_has_binary(env_name: str, binary: str) -> bool:
    prefix = conda_env_prefix(env_name)
    if prefix is None:
        return False
    path = prefix / "bin" / binary
    return path.is_file() and os.access(path, os.X_OK)


def conda_has_module(env_name: str, module: str) -> bool:
    import conda_cli
    result = subprocess.run(
        conda_cli.wrap_env_run(env_name, ["python", "-c", f"import {module}"]),
        capture_output=True, text=True, timeout=60,
    )
    return result.returncode == 0


def ensure_base_env(logger: Callable[[str], None] | None = None) -> None:
    log = logger or (lambda message: print(message, flush=True))
    if conda_env_exists(BASE_ENV_NAME):
        log(f"conda env {BASE_ENV_NAME} already exists")
        return
    if not ENV_FILE.is_file():
        raise FileNotFoundError(ENV_FILE)
    log(f"creating {BASE_ENV_NAME} from {ENV_FILE}")
    import conda_cli
    result = None
    for cmd in conda_cli.env_create_commands(str(ENV_FILE)):
        log(f"  {' '.join(cmd)}")
        result = subprocess.run(cmd, text=True)
        if result.returncode == 0:
            return
    raise RuntimeError(f"failed to create {BASE_ENV_NAME}")


def _run_conda_install(packages: list[str], log: Callable[[str], None], *, libmamba: bool = True) -> int:
    import conda_cli
    cmd = conda_cli.install_command(BASE_ENV_NAME, packages, libmamba=libmamba)
    log(f"  {' '.join(cmd)}")
    try:
        result = subprocess.run(
            cmd, text=True, timeout=CONDA_PACKAGE_TIMEOUT, env=conda_cli.install_env(),
        )
    except subprocess.TimeoutExpired:
        log(f"  timed out after {CONDA_PACKAGE_TIMEOUT}s")
        return 124
    return result.returncode


def install_conda_extras(logger: Callable[[str], None] | None = None) -> None:
    log = logger or (lambda message: print(message, flush=True))
    import conda_cli
    ensure_base_env(log)
    missing = [
        package for package, binary in CONDA_EXTRA_PACKAGES
        if not conda_has_binary(BASE_ENV_NAME, binary)
    ]
    if not missing:
        log("conda extras already present")
    else:
        log(f"installing extras with {Path(conda_cli.solver_cli()).name}: {', '.join(missing)}")
        status = _run_conda_install(missing, log)
        if status != 0:
            log("batch install failed; installing conda-libmamba-solver then retrying")
            _run_conda_install(["conda-libmamba-solver"], log, libmamba=False)
            status = _run_conda_install(missing, log)
        if status != 0:
            log("batch install still failing; trying one package at a time")
            for package in missing:
                if conda_has_binary(BASE_ENV_NAME, dict(CONDA_EXTRA_PACKAGES)[package]):
                    continue
                pkg_status = _run_conda_install([package], log)
                if pkg_status != 0:
                    log(f"  skipped {package}: exit {pkg_status}")
                else:
                    log(f"  installed {package}")
    missing_pip = [pkg for pkg in CONDA_EXTRA_PIP if not conda_has_module(BASE_ENV_NAME, pkg.replace("-", "_"))]
    if not missing_pip:
        log("  pip extras already present")
    else:
        pip = subprocess.run(
            conda_cli.wrap_env_run(BASE_ENV_NAME, ["pip", "install", *missing_pip]),
            text=True, timeout=CONDA_PACKAGE_TIMEOUT,
        )
        if pip.returncode != 0:
            log("  pip extras warning: caper/huggingface_hub install failed")
    if not conda_has_binary(BASE_ENV_NAME, "idr"):
        log("bioconda idr has no Python 3.11 build; pip installing kundajelab/idr")
        if not conda_has_module(BASE_ENV_NAME, "Cython"):
            log("  installing Cython so idr.pyx is rebuilt for Python 3.11")
            cython = subprocess.run(
                conda_cli.wrap_env_run(BASE_ENV_NAME, ["pip", "install", IDR_CYTHON_SPEC]),
                text=True, timeout=CONDA_PACKAGE_TIMEOUT,
            )
            if cython.returncode != 0:
                log("  skipped idr: Cython install failed")
                return
        pip = subprocess.run(
            conda_cli.wrap_env_run(BASE_ENV_NAME, ["pip", "install", *IDR_PIP_ARGS]),
            text=True, timeout=CONDA_PACKAGE_TIMEOUT,
        )
        if pip.returncode != 0:
            log("  skipped idr: pip install failed")
        else:
            log("  installed idr")


def caper_smoke_marker(cache_dir: Path) -> Path:
    return cache_dir / CAPER_SMOKE_DIR / "SUCCEEDED"


def caper_smoke_ok(work: Path, result: subprocess.CompletedProcess) -> bool:
    parts = [result.stdout or "", result.stderr or ""]
    for name in ("caper.stderr", "cromwell.stdout"):
        path = work / name
        if path.is_file():
            parts.append(path.read_text(encoding="utf-8", errors="replace"))
    combined = "\n".join(parts)
    if "status=Succeeded" in combined or "Cromwell finished successfully" in combined:
        return True
    meta = work / "metadata.json"
    if not meta.is_file() or meta.stat().st_size == 0:
        return False
    try:
        data = json.loads(meta.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return False
    status = str(data.get("status") or "").lower()
    return status == "succeeded" or bool(data.get("id") and data.get("outputs"))


def probe_caper_launch(
    cache_dir: Path,
    logger: Callable[[str], None] | None = None,
    *,
    force: bool = False,
) -> None:
    """Run a hello.wdl through Caper local backend to catch Cromwell start failures."""
    log = logger or (lambda message: print(message, flush=True))
    marker = caper_smoke_marker(cache_dir)
    if marker.is_file() and not force:
        log(f"caper smoke already succeeded ({marker})")
        return
    if not conda_env_exists(BASE_ENV_NAME):
        log("  skip caper smoke: conda env missing")
        return
    if not conda_has_binary(BASE_ENV_NAME, "java"):
        log("  skip caper smoke: java not in env")
        return
    if not conda_has_module(BASE_ENV_NAME, "caper"):
        log("  skip caper smoke: caper not installed")
        return
    import conda_cli
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    work = cache_dir / CAPER_SMOKE_DIR / stamp
    work.mkdir(parents=True, exist_ok=True)
    wdl = work / "hello.wdl"
    wdl.write_text(HELLO_WDL)
    db = work / "cromwell-db"
    out = work / "out"
    cromwell_out = work / "cromwell.stdout"
    meta = work / "metadata.json"
    cmd = conda_cli.wrap_env_run(BASE_ENV_NAME, [
        "caper", "run", str(wdl),
        "-b", "local",
        "--ignore-womtool",
        "--disable-call-caching",
        "--file-db", str(db),
        "--local-out-dir", str(out),
        "--cromwell-stdout", str(cromwell_out),
        "-m", str(meta),
    ])
    log(f"  {' '.join(cmd)}")
    try:
        result = subprocess.run(
            cmd, cwd=str(work), text=True, timeout=CAPER_SMOKE_TIMEOUT,
            capture_output=True,
        )
    except subprocess.TimeoutExpired as exc:
        (work / "caper.stdout").write_text(exc.stdout or "")
        (work / "caper.stderr").write_text(
            f"{exc.stderr or ''}\nTIMEOUT after {CAPER_SMOKE_TIMEOUT}s\n"
        )
        log(f"  caper smoke timed out after {CAPER_SMOKE_TIMEOUT}s; logs in {work}")
        return
    (work / "caper.stdout").write_text(result.stdout or "")
    (work / "caper.stderr").write_text(result.stderr or "")
    if caper_smoke_ok(work, result):
        marker.write_text(f"{stamp}\n{work}\n", encoding="utf-8")
        log(f"  caper smoke succeeded: {work}")
        return
    log(f"  caper smoke failed (exit {result.returncode}); logs in {work}")


def docker_available() -> bool:
    return shutil.which("docker") is not None


def docker_image_present(image: str) -> bool:
    if not docker_available():
        return False
    result = subprocess.run(
        ["docker", "image", "inspect", image],
        capture_output=True, text=True,
    )
    return result.returncode == 0


def pull_docker_images(
    images: Iterable[str] = DOCKER_IMAGES,
    logger: Callable[[str], None] | None = None,
) -> None:
    log = logger or (lambda message: print(message, flush=True))
    if not docker_available():
        log("docker not on PATH; skip image pull (Singularity SIF can still be cached)")
        return
    extra = [
        item.strip()
        for item in os.environ.get("COMPBIO_DOCKER_MIRRORS", "").split(",")
        if item.strip()
    ]
    mirrors = extra or list(DEFAULT_DOCKER_MIRRORS)
    for image in images:
        if docker_image_present(image):
            log(f"docker image already present: {image}")
            continue
        candidates = [image] + [f"{mirror}/{image}" for mirror in mirrors]
        pulled = False
        for candidate in candidates:
            log(f"docker pull {candidate}")
            try:
                result = subprocess.run(["docker", "pull", candidate], text=True, timeout=1800)
            except subprocess.TimeoutExpired:
                log(f"  timed out pulling {candidate}")
                continue
            if result.returncode == 0:
                if candidate != image:
                    subprocess.run(["docker", "tag", candidate, image], check=False)
                pulled = True
                break
        if not pulled:
            log(f"WARNING: failed to pull {image}")


def singularity_bin() -> str | None:
    return shutil.which("apptainer") or shutil.which("singularity")


def build_sif_from_docker(dest: Path, docker_image: str, log: Callable[[str], None]) -> bool:
    """Convert a local Docker image to SIF when the published SIF URL is gone."""
    tool = singularity_bin()
    if not tool:
        log("  apptainer/singularity not on PATH; cannot convert docker image")
        return False
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".building")
    if tmp.exists():
        tmp.unlink()
    sources = []
    if docker_image_present(docker_image):
        sources.append(f"docker-daemon://{docker_image}")
    sources.append(f"docker://{docker_image}")
    for source in sources:
        log(f"  {Path(tool).name} build from {source}")
        result = subprocess.run([tool, "build", "--force", str(tmp), source], text=True)
        if result.returncode == 0 and is_complete_file(tmp):
            tmp.replace(dest)
            return True
    if tmp.exists():
        tmp.unlink()
    return False


def pull_singularity_images(
    cache_dir: Path,
    *,
    force: bool = False,
    logger: Callable[[str], None] | None = None,
) -> None:
    log = logger or (lambda message: print(message, flush=True))
    dest_dir = cache_dir / "singularity"
    dest_dir.mkdir(parents=True, exist_ok=True)
    for name, url, docker_image in SINGULARITY_IMAGES:
        dest = dest_dir / name
        log(f"singularity image {dest}")
        if is_complete_file(dest) and not force:
            log("  cached")
            continue
        try:
            status = download_url(url, dest, force=force)
            log(f"  {status}")
            continue
        except Exception as exc:
            log(f"  WARNING: {exc}")
        if docker_image and build_sif_from_docker(dest, docker_image, log):
            log("  built from docker")
        else:
            log("  WARNING: no SIF available; Docker image is enough if you run with Docker")


def write_encode_tsv(dest_dir: Path, files: dict[str, Path]) -> Path:
    tsv = dest_dir / "hg38.tsv"
    rows = [
        ("genome_name", "hg38"),
        ("mito_chr_name", "chrM"),
        ("gensz", "hs"),
        ("regex_bfilt_peak_chr_name", r"chr[\dXY]+"),
    ]
    for key, path in files.items():
        rows.append((key, str(path)))
    tsv.write_text("".join(f"{key}\t{value}\n" for key, value in rows))
    urls = dest_dir / "hg38.urls.tsv"
    urls.write_text(
        "genome_name\thg38\n"
        + "".join(f"{key}\t{url}\n" for key, url in ENCODE_HG38_TSV)
    )
    return tsv


def cache_genomes(
    cache_dir: Path,
    *,
    force: bool = False,
    logger: Callable[[str], None] | None = None,
) -> None:
    log = logger or (lambda message: print(message, flush=True))
    encode_dir = cache_dir / "genomes" / "encode-atac-hg38"
    hg38_dir = cache_dir / "genomes" / "hg38"
    encode_dir.mkdir(parents=True, exist_ok=True)
    hg38_dir.mkdir(parents=True, exist_ok=True)
    local_files: dict[str, Path] = {}
    for key, url in ENCODE_HG38_TSV:
        dest = encode_dir / url_filename(url)
        log(f"genome {key}: {dest.name}")
        try:
            status = download_url(url, dest, force=force)
            log(f"  {status}")
            local_files[key] = dest
        except Exception as exc:
            log(f"  WARNING: {exc}")
            continue
        if key in {"bowtie2_idx_tar", "bowtie2_mito_idx_tar", "bwa_idx_tar", "bwa_mito_idx_tar"}:
            extract_name = {
                "bowtie2_idx_tar": "bowtie2",
                "bowtie2_mito_idx_tar": "bowtie2_mito",
                "bwa_idx_tar": "bwa",
                "bwa_mito_idx_tar": "bwa_mito",
            }[key]
            try:
                extract_tar(dest, encode_dir / extract_name)
                if key == "bowtie2_idx_tar":
                    extract_tar(dest, hg38_dir / "bowtie2")
                if key == "bwa_idx_tar":
                    extract_tar(dest, hg38_dir / "bwa")
            except Exception as exc:
                log(f"  extract warning: {exc}")
    if "ref_fa" in local_files:
        gz = local_files["ref_fa"]
        fa = hg38_dir / "hg38.fa"
        link = hg38_dir / "hg38.fa.gz"
        if not link.exists():
            try:
                link.symlink_to(gz)
            except OSError:
                shutil.copy2(gz, link)
        if not fa.exists() or force:
            log(f"decompressing {gz.name} -> {fa}")
            subprocess.run(["gzip", "-dc", str(gz)], check=True, stdout=fa.open("wb"))
        samtools = shutil.which("samtools")
        if samtools and fa.exists() and not (hg38_dir / "hg38.fa.fai").exists():
            subprocess.run([samtools, "faidx", str(fa)], check=False)
    if local_files:
        tsv = write_encode_tsv(encode_dir, local_files)
        log(f"wrote {tsv}")
    ebi_dir = cache_dir / "ebi"
    ebi_dir.mkdir(parents=True, exist_ok=True)
    for name, url in EBI_FILES:
        dest = ebi_dir / name
        log(f"ebi {name}")
        try:
            log(f"  {download_url(url, dest, force=force)}")
        except Exception as exc:
            log(f"  WARNING: {exc}")


def huggingface_bin() -> str | None:
    """Prefer `hf`; the old huggingface-cli stub now exits without downloading."""
    return shutil.which("hf") or shutil.which("huggingface-cli")


def hf_repo_cached(cache_dir: Path, repo: str) -> bool:
    slug = "models--" + repo.replace("/", "--")
    hubs = [cache_dir / "models" / "huggingface"]
    env_home = os.environ.get("HF_HOME")
    if env_home:
        hubs.append(Path(env_home))
    for hub in hubs:
        snapshots = hub / "hub" / slug / "snapshots"
        if snapshots.is_dir() and any(
            path.is_dir() and any(path.iterdir()) for path in snapshots.iterdir()
        ):
            return True
    return False


def cache_models(
    cache_dir: Path,
    *,
    force: bool = False,
    logger: Callable[[str], None] | None = None,
) -> None:
    log = logger or (lambda message: print(message, flush=True))
    apply_cache_environ(cache_dir)
    hf_bin = huggingface_bin()
    for repo in HF_MODELS:
        log(f"huggingface {repo}")
        if hf_repo_cached(cache_dir, repo) and not force:
            log("  cached")
            continue
        if hf_bin:
            cmd = [hf_bin, "download", repo]
            if force:
                cmd.append("--force-download")
            result = subprocess.run(cmd, text=True)
            if result.returncode != 0:
                log(f"  WARNING: {Path(hf_bin).name} failed for {repo}")
        else:
            log("  hf not found; set HF_HOME and download later")
    splice_dir = cache_dir / "models" / "openspliceai" / "OSAIMANE-10000nt"
    splice_dir.mkdir(parents=True, exist_ok=True)
    splice_host_down = False
    for name, urls in OPENSPLICEAI_MODELS:
        dest = splice_dir / name
        log(f"openspliceai {name}")
        if dest.is_file() and dest.stat().st_size == OPENSPLICEAI_PT_SIZE and not force:
            log("  cached")
            continue
        if dest.exists() and dest.stat().st_size != OPENSPLICEAI_PT_SIZE:
            dest.unlink()
        if splice_host_down:
            log("  skipped: OpenSpliceAI hosts previously returned an error")
            continue
        last_error = None
        for url in (urls if isinstance(urls, (tuple, list)) else (urls,)):
            try:
                log(f"  {download_url(url, dest, force=force)}")
                last_error = None
                break
            except Exception as exc:
                last_error = exc
        if last_error is not None:
            log(f"  WARNING: {last_error}")
            splice_host_down = True
            log("  skipping remaining OpenSpliceAI checkpoints")


def cache_kraken(
    cache_dir: Path,
    *,
    force: bool = False,
    logger: Callable[[str], None] | None = None,
) -> None:
    log = logger or (lambda message: print(message, flush=True))
    dest = cache_dir / "kraken2" / "k2_standard_08gb_20240605.tar.gz"
    log(f"kraken2 {dest}")
    try:
        status = download_url(KRAKEN_STANDARD_8GB, dest, force=force)
        log(f"  {status}")
        extract_tar(dest, cache_dir / "kraken2" / "k2_standard_08gb")
    except Exception as exc:
        log(f"  WARNING: {exc}")


def cache_has_content(cache_dir: Path) -> bool:
    if not cache_dir.is_dir():
        return False
    return any(cache_dir.iterdir())


def write_index(cache_dir: Path) -> Path:
    cache_dir.mkdir(parents=True, exist_ok=True)
    lines = [
        "# CompBioBench local cache",
        "",
        "Prefer these local paths over downloading. The question workspace also",
        f"exposes this tree as `{CACHE_LINK_NAME}/`.",
        "",
        f"- Absolute path: `{cache_dir}`",
        f"- `COMPBIO_CACHE_DIR` and Hugging Face / Singularity cache env vars point here.",
        "",
        "## Containers",
        "",
    ]
    for image in DOCKER_IMAGES:
        present = "present" if docker_image_present(image) else "not pulled"
        lines.append(f"- Docker `{image}` ({present})")
    for name, _url, _docker in SINGULARITY_IMAGES:
        path = cache_dir / "singularity" / name
        state = "ready" if path.exists() else "missing"
        lines.append(f"- Singularity `{path}` ({state})")
    lines += ["", "## Genomes", ""]
    hg38_fa = cache_dir / "genomes" / "hg38" / "hg38.fa"
    lines.append(f"- hg38 FASTA: `{hg38_fa}` ({'ready' if hg38_fa.exists() else 'missing'})")
    bt2 = cache_dir / "genomes" / "hg38" / "bowtie2"
    lines.append(f"- bowtie2 index dir: `{bt2}` ({'ready' if bt2.exists() else 'missing'})")
    tsv = cache_dir / "genomes" / "encode-atac-hg38" / "hg38.tsv"
    lines.append(
        f"- ENCODE ATAC `atac.genome_tsv`: `{tsv}` ({'ready' if tsv.exists() else 'missing'})"
    )
    lines += ["", "## Models", ""]
    hf = cache_dir / "models" / "huggingface"
    lines.append(f"- Hugging Face `HF_HOME`: `{hf}`")
    for repo in HF_MODELS:
        lines.append(f"- `{repo}` (huggingface hub cache)")
    splice = cache_dir / "models" / "openspliceai" / "OSAIMANE-10000nt"
    lines.append(f"- OpenSpliceAI OSAIMANE-10000nt: `{splice}`")
    ebi = cache_dir / "ebi" / "gencode.vM31.annotation.gtf.gz"
    lines += [
        "",
        "## Annotations",
        "",
        f"- GENCODE mouse M31 GTF: `{ebi}` ({'ready' if ebi.exists() else 'missing'})",
        "",
        "## Conda",
        "",
        f"- Base env `{BASE_ENV_NAME}` is cloned per question. Warmup installs bowtie2,",
        "  macs2, idr, picard, cutadapt, chromap, kraken2, STAR, and caper into it.",
        "",
        "## ENCODE ATAC / Caper",
        "",
        "Timeouts on encode-atac-pipeline-q1 have been Caper/Cromwell **launch**",
        "failures (HSQLDB init, no workflow UUID, no `cromwell-executions/`), not",
        "alignment runtime. Successful runs finished in about 25–30 minutes once",
        "Cromwell started. Do not sit on a hung launch until the question timeout.",
        "",
        "- Use a unique Cromwell/HSQLDB file and output directory in this workspace.",
        "- Persist Caper and Cromwell stderr to files here; a killed run otherwise",
        "  leaves no error log.",
        "- If there is no workflow UUID and no `cromwell-executions/` within 5",
        "  minutes, abort that launch and retry with a fresh DB.",
        "- Keep the pipeline timeout several minutes below the question limit so",
        "  you can extract `qc.json` and emit the final answer.",
        "- Do not treat leftover `atac_out/` or `qc.json` as success unless this",
        "  attempt produced them.",
        "",
        f"- Warmup `caper` step: {caper_smoke_status(cache_dir)}.",
        "",
        "If a file is missing, fetch it from the internet as usual.",
        "",
    ]
    path = cache_dir / "INDEX.md"
    path.write_text("\n".join(lines))
    manifest = {
        "cache_dir": str(cache_dir),
        "updated": datetime.now(timezone.utc).isoformat(),
        "docker_images": list(DOCKER_IMAGES),
        "hf_models": list(HF_MODELS),
        "encode_hg38_files": [name for name, _url in ENCODE_HG38_TSV],
    }
    (cache_dir / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    return path


def mount_cache(work_dir: str | Path, cache_dir: Path | None = None) -> bool:
    """Symlink the shared cache into the question workspace. Returns True if mounted."""
    cache_dir = cache_dir or default_cache_dir()
    if not cache_dir.is_dir():
        return False
    dest = Path(work_dir) / CACHE_LINK_NAME
    if dest.is_symlink():
        dest.unlink()
    elif dest.exists():
        return dest.is_dir()
    dest.symlink_to(cache_dir, target_is_directory=True)
    return True


def prompt_cache_instructions() -> list[str]:
    return [
        f"- A read-only local cache is mounted at {CACHE_LINK_NAME}/ "
        "(same tree as $COMPBIO_CACHE_DIR). Prefer it over re-downloading "
        "genomes, aligner indexes, containers, and models. See "
        f"{CACHE_LINK_NAME}/INDEX.md.",
        "- If a needed file is missing from the cache, get it from the internet.",
        "- Session rules for this workspace are in AGENTS.md and .wisp/WISP.md "
        "(Caper/Cromwell launch, local_cache, one-line answer).",
    ]


def write_network_report(cache_dir: Path, results: list[ProbeResult]) -> Path:
    cache_dir.mkdir(parents=True, exist_ok=True)
    path = cache_dir / "network-check.json"
    payload = {
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "results": [asdict(item) for item in results],
    }
    path.write_text(json.dumps(payload, indent=2) + "\n")
    return path


def format_network_results(results: list[ProbeResult]) -> str:
    lines = ["Network precheck:"]
    for item in results:
        mark = "OK" if item.ok else "FAIL"
        extra = f" HTTP {item.status}" if item.status else ""
        err = f" ({item.error})" if item.error and not item.ok else ""
        lines.append(
            f"  [{mark}] {item.family}/{item.name}{extra} {item.elapsed_ms}ms{err}"
        )
    return "\n".join(lines)


def caper_smoke_status(cache_dir: Path) -> str:
    marker = caper_smoke_marker(cache_dir)
    if marker.is_file():
        first = marker.read_text(encoding="utf-8").splitlines()[:1]
        return f"succeeded {first[0]}" if first else "succeeded"
    root = cache_dir / CAPER_SMOKE_DIR
    if root.is_dir() and any(root.iterdir()):
        return f"failed or incomplete; see {root}"
    return "not run"


def selected_steps(only: str, skip: str) -> set[str]:
    chosen = set(STEPS)
    if only.strip():
        chosen = {item.strip() for item in only.split(",") if item.strip()}
        unknown = chosen - set(STEPS)
        if unknown:
            raise ValueError(f"Unknown warmup step(s): {', '.join(sorted(unknown))}")
    skipped = {item.strip() for item in skip.split(",") if item.strip()}
    unknown = skipped - set(STEPS)
    if unknown:
        raise ValueError(f"Unknown warmup step(s): {', '.join(sorted(unknown))}")
    return chosen - skipped


def _run_network_step(cache_dir: Path, log, args) -> None:
    results = network_precheck()
    log(format_network_results(results))
    write_network_report(cache_dir, results)
    failed = required_network_failed(results)
    if failed and getattr(args, "strict_network", False):
        names = ", ".join(f"{item.family}/{item.name}" for item in failed)
        raise SystemExit(f"network precheck failed: {names}")
    if failed:
        log("WARNING: some required endpoints failed; prefer the local cache")
    if maybe_enable_hf_mirror(results):
        log("HINT: huggingface.co failed; using HF_ENDPOINT=https://hf-mirror.com for this process")


def print_status(cache_dir: Path) -> None:
    print(f"cache dir: {cache_dir}")
    print(f"exists: {cache_dir.is_dir()}")
    print(f"INDEX.md: {(cache_dir / 'INDEX.md').is_file()}")
    for image in DOCKER_IMAGES:
        print(f"docker {image}: {'yes' if docker_image_present(image) else 'no'}")
    for name, _url, _docker in SINGULARITY_IMAGES:
        path = cache_dir / "singularity" / name
        print(f"sif {name}: {'yes' if is_complete_file(path) else 'no'}")
    print(f"hg38.fa: {'yes' if is_complete_file(cache_dir / 'genomes' / 'hg38' / 'hg38.fa') else 'no'}")
    print(f"encode tsv: {'yes' if is_complete_file(cache_dir / 'genomes' / 'encode-atac-hg38' / 'hg38.tsv') else 'no'}")
    for key, url in ENCODE_HG38_TSV:
        path = cache_dir / "genomes" / "encode-atac-hg38" / url_filename(url)
        print(f"genome {key}: {'yes' if is_complete_file(path) else 'no'}")
    for repo in HF_MODELS:
        print(f"hf {repo}: {'yes' if hf_repo_cached(cache_dir, repo) else 'no'}")
    splice = cache_dir / "models" / "openspliceai" / "OSAIMANE-10000nt"
    for name, _urls in OPENSPLICEAI_MODELS:
        path = splice / name
        ok = path.is_file() and path.stat().st_size == OPENSPLICEAI_PT_SIZE
        print(f"openspliceai {name}: {'yes' if ok else 'no'}")
    print(f"conda {BASE_ENV_NAME}: {'yes' if conda_env_exists(BASE_ENV_NAME) else 'no'}")
    if conda_env_exists(BASE_ENV_NAME):
        for package, binary in (*CONDA_EXTRA_PACKAGES, ("idr", "idr")):
            print(f"  {package}: {'yes' if conda_has_binary(BASE_ENV_NAME, binary) else 'no'}")
    print(f"caper smoke: {caper_smoke_status(cache_dir)}")


def cmd_warmup(args) -> None:
    if getattr(args, "cache_dir", None):
        os.environ["COMPBIO_CACHE_DIR"] = str(Path(args.cache_dir).expanduser())
    cache_dir = apply_cache_environ()
    cache_dir.mkdir(parents=True, exist_ok=True)
    log = lambda message: print(message, flush=True)
    log(f"CompBioBench cache: {cache_dir}")

    if getattr(args, "status", False):
        print_status(cache_dir)
        return

    steps = selected_steps(getattr(args, "only", ""), getattr(args, "skip", ""))
    dry = getattr(args, "dry_run", False)
    force = getattr(args, "force", False)
    if dry:
        log(f"dry-run steps: {', '.join(step for step in STEPS if step in steps)}")
        for probe in network_probes():
            log(f"  probe {probe.family}/{probe.name} {probe.url}")
        for image in DOCKER_IMAGES:
            log(f"  docker {image}")
        for name, url, docker in SINGULARITY_IMAGES:
            log(f"  sif {name} {url} (fallback docker {docker})")
        for key, url in ENCODE_HG38_TSV:
            log(f"  genome {key} {url_filename(url)}")
        for repo in HF_MODELS:
            log(f"  hf {repo}")
        log("  caper run hello.wdl -b local (unique HSQLDB, 5 min timeout)")
        return

    runners = {
        "network": lambda: _run_network_step(cache_dir, log, args),
        "docker": lambda: pull_docker_images(logger=log),
        "singularity": lambda: pull_singularity_images(cache_dir, force=force, logger=log),
        "genomes": lambda: cache_genomes(cache_dir, force=force, logger=log),
        "models": lambda: cache_models(cache_dir, force=force, logger=log),
        "conda": lambda: install_conda_extras(log),
        "caper": lambda: probe_caper_launch(cache_dir, logger=log, force=force),
    }
    for step in STEPS:
        if step in steps:
            runners[step]()
    if getattr(args, "with_kraken", False):
        cache_kraken(cache_dir, force=force, logger=log)

    index = write_index(cache_dir)
    log(f"wrote {index}")
    log("warmup complete")
    log("Re-run the same command to retry missing items; complete files, images, and tools are skipped.")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Pre-install CompBioBench conda extras and cache genomes, containers, and models"
    )
    add_warmup_arguments(parser)
    return parser


def add_warmup_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--cache-dir", default=None, help="Override COMPBIO_CACHE_DIR")
    parser.add_argument(
        "--only", default="",
        help="Comma-separated steps: network,conda,docker,singularity,genomes,models,caper",
    )
    parser.add_argument("--skip", default="", help="Comma-separated steps to skip")
    parser.add_argument("--with-kraken", action="store_true", help="Also cache an 8GB Kraken2 DB")
    parser.add_argument("--dry-run", action="store_true", help="Print planned work and exit")
    parser.add_argument("--status", action="store_true", help="Show what is already cached")
    parser.add_argument("--strict-network", action="store_true", help="Exit 1 if a required endpoint fails")
    parser.add_argument("--force", action="store_true", help="Re-download files that already exist")


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    cmd_warmup(args)


if __name__ == "__main__":
    main()
