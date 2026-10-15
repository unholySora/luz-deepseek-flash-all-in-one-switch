"""Build a complete Docker/OCI save archive from the pinned LuZ base and delta.

Uses Python standard library only. Never extracts tar paths or invokes Docker.
The required base is the OCI-format 0.3.1-r3 package, not a generic CUDA image.
"""
import argparse
import hashlib
import io
import json
import pathlib
import re
import sys
import tarfile


def digest_path(digest):
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
        raise ValueError("Invalid SHA256 descriptor")
    return "blobs/sha256/" + digest.split(":", 1)[1]


class HashReader:
    def __init__(self, source):
        self.source = source
        self.hasher = hashlib.sha256()
        self.bytes = 0

    def read(self, size=-1):
        data = self.source.read(size)
        self.hasher.update(data)
        self.bytes += len(data)
        return data


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", required=True, type=pathlib.Path)
    parser.add_argument("--delta", type=pathlib.Path,
                        default=pathlib.Path(__file__).resolve().parents[1] / "image-delta")
    parser.add_argument("--output", help="New complete .tar path, or - for stdout")
    parser.add_argument("--verify-only", action="store_true")
    args = parser.parse_args()
    if args.verify_only == bool(args.output):
        parser.error("Specify exactly one of --output or --verify-only")
    plan = json.loads((args.delta / "delta.json").read_text())
    if plan["format"] != "luz-switch-layer-delta-v1":
        raise ValueError("Unsupported delta format")

    def delta_blob(digest):
        data = (args.delta / digest_path(digest)).read_bytes()
        if hashlib.sha256(data).hexdigest() != digest.split(":")[1]:
            raise ValueError("Delta blob checksum mismatch: " + digest)
        return data

    index_data = delta_blob(plan["public_index_digest"])
    index = json.loads(index_data)
    manifest = json.loads(delta_blob(plan["public_manifest_digest"]))
    config = json.loads(delta_blob(plan["public_config_digest"]))
    assert index["manifests"][0]["digest"] == plan["public_manifest_digest"]
    assert manifest["config"]["digest"] == plan["public_config_digest"]
    assert config["rootfs"]["diff_ids"] == plan["public_diff_ids"]
    assert config["architecture"] == "arm64" and config["os"] == "linux"
    assert manifest["layers"][:plan["base_layer_count"]] == plan["base_layers"]
    assert config["rootfs"]["diff_ids"][:plan["base_layer_count"]] == plan["base_diff_ids"]
    assert len(config["rootfs"]["diff_ids"]) == len(manifest["layers"])
    # Validate all small/new blobs before opening the output.
    added = {item["digest"]: item for item in plan["added_layers"]}
    import gzip
    for digest, item in added.items():
        data = delta_blob(digest)
        assert len(data) == item["bytes"]
        assert "sha256:" + hashlib.sha256(gzip.decompress(data)).hexdigest() == item["diff_id"]

    descriptors = {d["digest"]: d["size"] for d in manifest["layers"]}
    descriptors[plan["public_config_digest"]] = manifest["config"]["size"]
    descriptors[plan["public_manifest_digest"]] = index["manifests"][0]["size"]
    descriptors[plan["public_index_digest"]] = len(index_data)
    output = None
    output_path = None
    partial = None
    receipt = {"format": plan["format"], "public_index_digest": plan["public_index_digest"],
               "public_config_digest": plan["public_config_digest"], "layer_count": len(manifest["layers"]),
               "base_layer_count": plan["base_layer_count"], "new_layer_count": len(added)}
    try:
        with tarfile.open(args.base, "r:*") as base:
            members = {m.name: m for m in base if m.isfile()}
            base_config_path = digest_path(plan["base_config_digest"])
            if base_config_path not in members:
                raise ValueError("Wrong base package: pinned base configuration blob is missing")
            base_config_data = base.extractfile(members[base_config_path]).read()
            if hashlib.sha256(base_config_data).hexdigest() != plan["base_config_digest"].split(":")[1]:
                raise ValueError("Base configuration checksum mismatch")
            assert json.loads(base_config_data)["rootfs"]["diff_ids"] == plan["base_diff_ids"]
            for desc in plan["base_layers"]:
                name = digest_path(desc["digest"])
                if name not in members or members[name].size != desc["size"]:
                    raise ValueError("Base layer representation differs or is missing: " + desc["digest"] +
                                     ". Use the original OCI-format 0.3.1-r3 package.")
            if args.output:
                if args.output == "-":
                    output = sys.stdout.buffer
                else:
                    output_path = pathlib.Path(args.output)
                    if output_path.exists():
                        raise FileExistsError("Refusing to overwrite output")
                    partial = output_path.with_name(output_path.name + ".partial")
                    output = partial.open("xb")
            archive = tarfile.open(fileobj=output, mode="w|", format=tarfile.PAX_FORMAT) if output else None

            def add(name, size, stream):
                if archive:
                    info = tarfile.TarInfo(name)
                    info.size = size
                    info.mode = 0o644
                    archive.addfile(info, stream)
                else:
                    while stream.read(8 * 1024 * 1024):
                        pass

            verified_bytes = 0
            try:
                for digest, size in sorted(descriptors.items()):
                    name = digest_path(digest)
                    use_delta = (args.delta / name).is_file()
                    stream = (args.delta / name).open("rb") if use_delta else base.extractfile(members[name])
                    with stream:
                        hashed = HashReader(stream)
                        add(name, size, hashed)
                        if hashed.bytes != size or hashed.hasher.hexdigest() != digest.split(":")[1]:
                            raise ValueError("Layer/config content checksum mismatch: " + digest)
                        verified_bytes += hashed.bytes
                docker_manifest = [{"Config": digest_path(plan["public_config_digest"]),
                                    "RepoTags": ["luz-switch:20261015"],
                                    "Layers": [digest_path(d["digest"]) for d in manifest["layers"]]}]
                metadata = {
                    "manifest.json": json.dumps(docker_manifest, separators=(",", ":")).encode(),
                    "index.json": json.dumps({"schemaVersion": 2, "mediaType": "application/vnd.oci.image.index.v1+json",
                                               "manifests": [{"mediaType": "application/vnd.oci.image.index.v1+json",
                                                              "digest": plan["public_index_digest"], "size": len(index_data),
                                                              "annotations": {"io.containerd.image.name": "docker.io/library/luz-switch:20261015",
                                                                              "org.opencontainers.image.ref.name": "20261015"}}]},
                                              separators=(",", ":")).encode(),
                    "oci-layout": b'{"imageLayoutVersion":"1.0.0"}',
                }
                for name, data in metadata.items():
                    add(name, len(data), io.BytesIO(data))
            finally:
                if archive:
                    archive.close()
            receipt["content_bytes_sha256_verified"] = verified_bytes
        if output and output is not sys.stdout.buffer:
            output.close()
            output = None
            # Destination was checked before writing; do not overwrite a file
            # created by another process while this large archive was built.
            if output_path.exists():
                raise FileExistsError("Output appeared during reconstruction; .partial retained")
            partial.rename(output_path)
        receipt["passed"] = True
        receipt["scope"] = "Archive assembly/checksum only; does not run inference or certify full KV recall."
        print(json.dumps(receipt), file=sys.stderr, flush=True)
    finally:
        if output and output is not sys.stdout.buffer:
            output.close()


if __name__ == "__main__":
    main()
