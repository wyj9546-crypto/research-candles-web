"""Validate a sealed public archive before extracting it for GitHub Pages."""
import gzip
import io
import hashlib
import json
import re
import sys
import tarfile
from pathlib import Path, PurePosixPath


def public_name(name):
    p = PurePosixPath(name)
    if not name or p.is_absolute() or ".." in p.parts or str(p) != name or "\\" in name or ":" in name:
        return False
    return (name in {"index.html", ".nojekyll", "data/manifest.json", "data/status.json"}
            or name.startswith("assets/") and p.suffix in {".js", ".css", ".svg", ".png", ".jpg", ".webp", ".ico", ".woff", ".woff2"}
            or re.fullmatch(r"data/builds/[a-f0-9]{16}/.+\.json(?:\.gz)?", name) is not None)


def verify_extract(path, expected_sha, destination):
    path, destination = Path(path), Path(destination)
    if destination.exists():
        raise ValueError("Extraction destination must be new")
    if not re.fullmatch(r"[a-f0-9]{64}", expected_sha):
        raise ValueError("Invalid expected archive hash")
    with path.open("rb") as stream:
        if hashlib.file_digest(stream, "sha256").hexdigest() != expected_sha:
            raise ValueError("Archive hash mismatch")
    # Gzip random access repeatedly inflates the preceding archive. Use two
    # sequential passes so validation remains linear in the archive size.
    with tarfile.open(path, "r|gz") as archive:
        receipt_file = next(iter(archive))
        if receipt_file.name != "release.json" or not receipt_file.isfile() or receipt_file.size > 10 * 1024 ** 2:
            raise ValueError("Oversized release receipt")
        receipt = json.load(archive.extractfile(receipt_file))
        if receipt.get("platform") != "github-pages" or receipt.get("schema") != 1:
            raise ValueError("Expected a GitHub Pages release")
        proofs = receipt["files"]
        if not {"index.html", ".nojekyll", "data/manifest.json"} <= set(proofs):
            raise ValueError("Missing public entrypoints")
        if any(not public_name(n) or type(p.get("bytes")) is not int or p["bytes"] < 0 for n, p in proofs.items()):
            raise ValueError("Invalid public member")
        if sum(p["bytes"] for p in proofs.values()) + receipt_file.size > 1000 ** 3:
            raise ValueError("Published site exceeds 1 GB")
        seen = {"release.json"}
        for member in archive:
            # TarFile's iterator yields its first member again after next().
            if member is receipt_file:
                continue
            if member.name in seen or not member.isfile():
                raise ValueError("Duplicate members, directories and links are forbidden")
            seen.add(member.name)
            name = member.name.removeprefix("site/")
            proof = proofs.get(name)
            if not member.name.startswith("site/") or proof is None or member.size != proof["bytes"]:
                raise ValueError("Invalid public member: " + name)
            with archive.extractfile(member) as stream:
                payload = stream.read()
            if hashlib.sha256(payload).hexdigest() != proof["sha256"]:
                raise ValueError("Member checksum mismatch: " + name)
            if name.endswith((".json", ".json.gz")):
                if name.endswith('.gz'):
                    with gzip.GzipFile(fileobj=io.BytesIO(payload)) as compressed:
                        payload = compressed.read(128 * 1024 ** 2 + 1)
                if len(payload) > 128 * 1024 ** 2:
                    raise ValueError('Oversized JSON shard')
                value = json.loads(payload)
                expected_build = name.split("/")[2] if name.startswith("data/builds/") else receipt["build_id"]
                if value.get("build_id") != expected_build:
                    raise ValueError("Mixed build versions: " + name)
            if name == ".nojekyll" and payload != b"":
                raise ValueError("Invalid .nojekyll marker")
        if seen != {"release.json", *("site/" + n for n in proofs)}:
            raise ValueError("Unexpected archive contents")
    # Extract only verified names; never invoke extractall on incoming files.
    destination.mkdir(parents=True)
    with tarfile.open(path, "r|gz") as archive:
        extracted = set()
        for member in archive:
            if member.name == "release.json":
                continue
            name = member.name.removeprefix("site/")
            if not member.isfile() or name not in proofs or name in extracted or member.name != "site/" + name:
                raise ValueError("Archive changed after validation")
            extracted.add(name)
            target = destination / name
            target.parent.mkdir(parents=True, exist_ok=True)
            checksum = hashlib.sha256()
            with archive.extractfile(member) as source, target.open("xb") as output:
                while chunk := source.read(1024 ** 2):
                    output.write(chunk)
                    checksum.update(chunk)
            if target.stat().st_size != proofs[name]["bytes"] or checksum.hexdigest() != proofs[name]["sha256"]:
                raise ValueError("Archive changed after validation")
        if extracted != set(proofs):
            raise ValueError("Archive changed after validation")
    print(json.dumps({"verified_build_id": receipt["build_id"], "files": len(proofs)}))


if __name__ == "__main__":
    verify_extract(sys.argv[1], sys.argv[2], sys.argv[3])
