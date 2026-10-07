"""Validate a sealed public archive before extracting it for GitHub Pages."""
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
            or re.fullmatch(r"data/builds/[a-f0-9]{16}/.+\.json", name) is not None)


def verify_extract(path, expected_sha, destination):
    path, destination = Path(path), Path(destination)
    if destination.exists():
        raise ValueError("Extraction destination must be new")
    if not re.fullmatch(r"[a-f0-9]{64}", expected_sha):
        raise ValueError("Invalid expected archive hash")
    with path.open("rb") as stream:
        if hashlib.file_digest(stream, "sha256").hexdigest() != expected_sha:
            raise ValueError("Archive hash mismatch")
    with tarfile.open(path, "r:gz") as archive:
        members = archive.getmembers()
        names = [m.name for m in members]
        if len(names) != len(set(names)) or any(not m.isfile() for m in members):
            raise ValueError("Duplicate members, directories and links are forbidden")
        receipt_file = archive.getmember("release.json")
        if receipt_file.size > 10 * 1024 ** 2:
            raise ValueError("Oversized release receipt")
        receipt = json.load(archive.extractfile(receipt_file))
        if receipt.get("platform") != "github-pages" or receipt.get("schema") != 1:
            raise ValueError("Expected a GitHub Pages release")
        proofs = receipt["files"]
        if set(names) != {"release.json", *("site/" + n for n in proofs)}:
            raise ValueError("Unexpected archive contents")
        if not {"index.html", ".nojekyll", "data/manifest.json"} <= set(proofs):
            raise ValueError("Missing public entrypoints")
        if sum(m.size for m in members) > 1000 ** 3:
            raise ValueError("Published site exceeds 1 GB")
        for name, proof in proofs.items():
            member = archive.getmember("site/" + name)
            if not public_name(name) or member.size != proof["bytes"]:
                raise ValueError("Invalid public member: " + name)
            with archive.extractfile(member) as stream:
                if hashlib.file_digest(stream, "sha256").hexdigest() != proof["sha256"]:
                    raise ValueError("Member checksum mismatch: " + name)
            if name.endswith(".json"):
                value = json.load(archive.extractfile(member))
                expected_build = name.split("/")[2] if name.startswith("data/builds/") else receipt["build_id"]
                if value.get("build_id") != expected_build:
                    raise ValueError("Mixed build versions: " + name)
        if archive.extractfile("site/.nojekyll").read() != b"":
            raise ValueError("Invalid .nojekyll marker")
        # Extract only verified names; never invoke extractall on incoming files.
        destination.mkdir(parents=True)
        for name in proofs:
            target = destination / name
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.extractfile("site/" + name) as source, target.open("xb") as output:
                while chunk := source.read(1024 ** 2):
                    output.write(chunk)
    print(json.dumps({"verified_build_id": receipt["build_id"], "files": len(proofs)}))


if __name__ == "__main__":
    verify_extract(sys.argv[1], sys.argv[2], sys.argv[3])
