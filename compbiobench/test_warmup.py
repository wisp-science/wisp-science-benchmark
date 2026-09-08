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


def main():
    test_cache_dir_and_environ()
    test_network_precheck_marks_failures()
    test_mount_cache_and_prompt()
    test_download_skips_existing_and_index()
    test_dry_run_and_runtime_cache()
    print("ok: warmup cache, network precheck, prompt, and local preference")


if __name__ == "__main__":
    main()
