"""Offline warmup/cache checks: python compbiobench/test_warmup.py."""

from __future__ import annotations

import io
import json
import os
from contextlib import redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from urllib.error import URLError
from urllib.request import Request

import conda_cli
import run_benchmark as rb
import warmup


class _FakeResponse:
    def __init__(self, status=200, body=b"ok"):
        self.status = status
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self, n=-1):
        return self._body[:n] if n >= 0 else self._body

    def getcode(self):
        return self.status


def test_micromamba_driver_selection():
    def fake_which(name):
        return {
            "micromamba": "/opt/micromamba",
            "mamba": None,
            "conda": None,
        }.get(name)

    with patch.dict(os.environ, {"COMPBIO_CONDA_CMD": ""}), \
         patch.object(conda_cli.shutil, "which", fake_which):
        os.environ.pop("COMPBIO_CONDA_CMD", None)
        assert conda_cli.solver_cli() == "/opt/micromamba"
        assert conda_cli.manage_cli() == "/opt/micromamba"
        assert conda_cli.wrap_env_run("compbio-benchmark", ["python", "-V"]) == [
            "/opt/micromamba", "run", "-n", "compbio-benchmark", "python", "-V",
        ]
        create = conda_cli.env_create_commands("environment.yml")
        assert create[0][:3] == ["/opt/micromamba", "env", "create"]
        assert "-y" in create[0]
        assert conda_cli.clone_command("compbio-benchmark", "clone1") == [
            "/opt/micromamba", "create", "-n", "clone1", "--clone", "compbio-benchmark", "-y",
        ]
        install = conda_cli.install_command("compbio-benchmark", ["bowtie2", "macs2"])
        assert install[:3] == ["/opt/micromamba", "install", "-n"]
        assert "--solver" not in install


def test_conda_preferred_over_micromamba():
    def fake_which(name):
        return {
            "conda": "/opt/conda/bin/conda",
            "mamba": "/opt/conda/bin/mamba",
            "micromamba": "/opt/micromamba",
        }.get(name)

    with patch.dict(os.environ, {"COMPBIO_CONDA_CMD": "micromamba"}):
        assert conda_cli.solver_cli() == "micromamba"
    with patch.dict(os.environ, {"COMPBIO_CONDA_CMD": ""}), \
         patch.object(conda_cli.shutil, "which", fake_which):
        os.environ.pop("COMPBIO_CONDA_CMD", None)
        assert conda_cli.solver_cli() == "/opt/conda/bin/mamba"
        assert conda_cli.manage_cli() == "/opt/conda/bin/conda"
        wrapped = conda_cli.wrap_env_run("e", ["x"])
        assert wrapped[:4] == ["/opt/conda/bin/conda", "run", "-n", "e"]
        assert "--live-stream" in wrapped

    def conda_only(name):
        return {"conda": "/opt/conda/bin/conda"}.get(name)

    with patch.dict(os.environ, {"COMPBIO_CONDA_CMD": ""}), \
         patch.object(conda_cli.shutil, "which", conda_only):
        os.environ.pop("COMPBIO_CONDA_CMD", None)
        cmd = conda_cli.install_command("compbio-benchmark", ["bowtie2"])
        assert cmd[:2] == ["/opt/conda/bin/conda", "install"]
        assert "--solver" in cmd and "libmamba" in cmd
        assert conda_cli.install_env().get("CONDA_SOLVER") == "libmamba"


def test_parse_env_paths_and_copy():
    assert conda_cli.parse_env_paths({
        "envs": ["/root", "/root/envs/compbio-benchmark"]
    })[-1].endswith("compbio-benchmark")
    assert conda_cli.parse_env_paths({
        "environments": [{"prefix": "/mm/envs/foo", "name": "foo"}]
    }) == ["/mm/envs/foo"]
    with TemporaryDirectory() as tmp:
        src = Path(tmp) / "src"
        dest = Path(tmp) / "dest"
        (src / "bin").mkdir(parents=True)
        (src / "bin" / "bowtie2").write_text("#!/bin/sh\n")
        conda_cli.copy_env_prefix(str(src), str(dest))
        assert (dest / "bin" / "bowtie2").is_file()


def test_cache_dir_and_environ():
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        with patch.dict(os.environ, {
            "COMPBIO_CACHE_DIR": str(root / "cache"),
            "COMPBIO_DATA_DIR": "",
        }):
            assert warmup.default_cache_dir() == (root / "cache").resolve()
        with patch.dict(os.environ, {
            "COMPBIO_DATA_DIR": str(root / "compbiobench-data"),
            "COMPBIO_CACHE_DIR": "",
        }):
            assert warmup.default_cache_dir() == (root / "compbiobench-cache").resolve()
        with patch.dict(os.environ, {"HF_ENDPOINT": ""}):
            env = warmup.cache_environ(root)
            assert env["COMPBIO_CACHE_DIR"] == str(root)
            assert env["HF_HOME"].endswith("models/huggingface")
            assert env["SINGULARITY_CACHEDIR"].endswith("singularity")
            assert "HF_ENDPOINT" not in env
        with patch.dict(os.environ, {"HF_ENDPOINT": "https://hf-mirror.com"}):
            assert warmup.cache_environ(root)["HF_ENDPOINT"] == "https://hf-mirror.com"


def test_network_precheck_marks_failures():
    def fake_open(request, timeout=0):
        url = request.full_url if isinstance(request, Request) else request
        if "encodeproject.org" in url:
            return _FakeResponse(200)
        raise URLError("blocked")

    with patch.object(warmup.urllib.request, "urlopen", fake_open):
        results = warmup.network_precheck(timeout=1)
    families = {item.family for item in results}
    assert {"ENCODE", "Hugging Face", "NCBI", "GEO", "EBI"} <= families
    assert all(item.ok for item in results if item.family == "ENCODE")
    failed = warmup.required_network_failed(results)
    assert {item.family for item in failed} >= {"Hugging Face", "NCBI", "GEO", "EBI"}
    with patch.dict(os.environ, {"HF_ENDPOINT": ""}):
        assert not warmup.maybe_enable_hf_mirror(results)
    mirror_ok = [
        warmup.ProbeResult("Hugging Face", "hub", "https://huggingface.co/", True, False, None, 1, "ssl"),
        warmup.ProbeResult("Hugging Face", "hf-mirror", "https://hf-mirror.com/", False, True, 200, 1, None),
    ]
    with patch.dict(os.environ, {"HF_ENDPOINT": ""}):
        assert warmup.maybe_enable_hf_mirror(mirror_ok)
        assert os.environ["HF_ENDPOINT"] == "https://hf-mirror.com"


def test_mount_cache_and_prompt():
    with TemporaryDirectory() as tmp:
        cache = Path(tmp) / "cache"
        cache.mkdir()
        (cache / "INDEX.md").write_text("cached hg38")
        work = Path(tmp) / "workspace"
        work.mkdir()
        assert warmup.mount_cache(work, cache)
        link = work / "local_cache"
        assert link.is_symlink()
        assert (link / "INDEX.md").read_text() == "cached hg38"
        prompt = rb.generate_prompt("q", None, ["a.fq"], 120, cache_mounted=True)
        assert "local_cache/" in prompt and "INDEX.md" in prompt
        assert "Prefer it over re-downloading" in prompt
        plain = rb.generate_prompt("q", None, ["a.fq"], 120, cache_mounted=False)
        assert "Get any files or tools you need from the internet." in plain
        assert "local_cache/" not in plain


def test_download_skips_existing_and_index():
    with TemporaryDirectory() as tmp:
        dest = Path(tmp) / "file.bin"
        dest.write_bytes(b"already")
        with patch.object(warmup.shutil, "which", return_value=None), \
             patch.object(warmup.urllib.request, "urlopen", side_effect=AssertionError("download")):
            assert warmup.download_url("https://example.test/file.bin", dest) == "cached"
        cache = Path(tmp) / "cache"
        (cache / "genomes" / "hg38").mkdir(parents=True)
        (cache / "genomes" / "hg38" / "hg38.fa").write_text(">chr1\nA\n")
        text = warmup.write_index(cache).read_text()
        assert "hg38 FASTA" in text
        assert "encodedcc/atac-seq-pipeline:v2.2.3" in text
        assert json.loads((cache / "manifest.json").read_text())["cache_dir"] == str(cache)
        report = warmup.write_network_report(cache, [
            warmup.ProbeResult(
                "ENCODE", "homepage", "https://www.encodeproject.org/", True, True, 200, 12, None
            )
        ])
        assert json.loads(report.read_text())["results"][0]["ok"] is True
        empty = Path(tmp) / "empty.bin"
        empty.write_bytes(b"")
        assert not warmup.is_complete_file(empty)


def test_singularity_falls_back_to_docker():
    name, url, docker = warmup.SINGULARITY_IMAGES[0]
    assert name.endswith(".sif")
    assert docker == "encodedcc/atac-seq-pipeline:v2.2.3"
    with TemporaryDirectory() as tmp:
        dest = Path(tmp) / name
        log = []
        with patch.object(warmup, "download_url", side_effect=RuntimeError("download failed (22): 404")), \
             patch.object(warmup, "build_sif_from_docker", return_value=True) as build:
            warmup.pull_singularity_images(Path(tmp), logger=log.append)
            build.assert_called_once()
            assert build.call_args.args[1] == docker


def test_openspliceai_skips_after_host_error():
    with TemporaryDirectory() as tmp:
        calls = []

        def boom(url, dest, force=False):
            calls.append(url)
            raise RuntimeError("download failed (22): 502")

        logs: list[str] = []
        with patch.object(warmup, "download_url", boom), \
             patch.object(warmup, "huggingface_bin", return_value=None), \
             patch.object(warmup, "HF_MODELS", ()):
            warmup.cache_models(Path(tmp), logger=logs.append)
        assert len(calls) == 3
        assert calls[0].startswith("https://raw.githubusercontent.com/Kuanhao-Chao/OpenSpliceAI/v0.0.5/")
        assert any("skipping remaining OpenSpliceAI" in line for line in logs)


def test_huggingface_prefers_hf_and_skips_cache():
    def fake_which(name):
        return {"hf": "/usr/bin/hf", "huggingface-cli": "/usr/bin/huggingface-cli"}.get(name)

    with patch.object(warmup.shutil, "which", fake_which):
        assert warmup.huggingface_bin() == "/usr/bin/hf"
    with TemporaryDirectory() as tmp:
        cache = Path(tmp)
        repo = "johahi/borzoi-replicate-0"
        assert not warmup.hf_repo_cached(cache, repo)
        snap = cache / "models" / "huggingface" / "hub" / "models--johahi--borzoi-replicate-0" / "snapshots" / "abc"
        snap.mkdir(parents=True)
        (snap / "config.json").write_text("{}")
        assert warmup.hf_repo_cached(cache, repo)


def test_dry_run_and_runtime_cache():
    with TemporaryDirectory() as tmp:
        args = warmup.build_parser().parse_args(["--cache-dir", tmp, "--dry-run"])
        buf = io.StringIO()
        with redirect_stdout(buf):
            warmup.cmd_warmup(args)
        out = buf.getvalue()
        assert "encodedcc/atac-seq-pipeline:v2.2.3" in out
        assert "kuleshov-group/caduceus" in out
        assert "ENCFF110MCL" in out
        assert "johahi/borzoi-replicate-0" in out
        assert "dry-run steps: network, docker, singularity, genomes, models, conda" in out

        cache = Path(tmp) / "ready"
        cache.mkdir()
        (cache / "INDEX.md").write_text("ready")
        logger = rb.logging.getLogger("warmup-test")
        with patch.dict(os.environ, {"COMPBIO_CACHE_DIR": str(cache)}), \
             patch.object(warmup, "network_precheck", return_value=[
                 warmup.ProbeResult(
                     "ENCODE", "homepage", "https://www.encodeproject.org/", True, True, 200, 1, None
                 )
             ]):
            path = rb.prepare_runtime_cache(logger)
            assert path == str(cache.resolve())
            assert os.environ["HF_HOME"].startswith(str(cache.resolve()))


def test_idr_uses_kundajelab_pip_not_bioconda():
    assert "kundajelab/idr" in warmup.IDR_PIP_SPEC
    assert "idr" not in dict(warmup.CONDA_EXTRA_PACKAGES)
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        class Result:
            returncode = 0
        return Result()

    with patch.object(warmup, "ensure_base_env"), \
         patch.object(warmup, "conda_has_binary", return_value=False), \
         patch.object(warmup, "conda_has_module", return_value=True), \
         patch.object(warmup, "_run_conda_install", return_value=0), \
         patch.object(warmup.subprocess, "run", fake_run):
        warmup.install_conda_extras(lambda _m: None)
    assert any(
        warmup.IDR_PIP_SPEC in cmd and "--no-build-isolation" in cmd
        for cmd in calls
    )


def test_idr_installs_cython_before_extension_build():
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        class Result:
            returncode = 0
        return Result()

    def has_module(_env, module):
        return module != "Cython"

    with patch.object(warmup, "ensure_base_env"), \
         patch.object(warmup, "conda_has_binary", return_value=False), \
         patch.object(warmup, "conda_has_module", has_module), \
         patch.object(warmup, "_run_conda_install", return_value=0), \
         patch.object(warmup.subprocess, "run", fake_run):
        warmup.install_conda_extras(lambda _m: None)
    cython_at = next(i for i, cmd in enumerate(calls) if warmup.IDR_CYTHON_SPEC in cmd)
    idr_at = next(i for i, cmd in enumerate(calls) if warmup.IDR_PIP_SPEC in cmd)
    assert cython_at < idr_at


def main():
    test_micromamba_driver_selection()
    test_conda_preferred_over_micromamba()
    test_parse_env_paths_and_copy()
    test_cache_dir_and_environ()
    test_network_precheck_marks_failures()
    test_mount_cache_and_prompt()
    test_download_skips_existing_and_index()
    test_singularity_falls_back_to_docker()
    test_openspliceai_skips_after_host_error()
    test_huggingface_prefers_hf_and_skips_cache()
    test_dry_run_and_runtime_cache()
    test_idr_uses_kundajelab_pip_not_bioconda()
    test_idr_installs_cython_before_extension_build()
    print("ok: warmup cache, network precheck, prompt, and local preference")


if __name__ == "__main__":
    main()
