"""Rebuild the offline page using its existing verified embedded ML assets."""
import base64
import hashlib
import json
import re
from pathlib import Path


def main():
    root = Path(__file__).resolve().parents[1]
    existing_path = root / "fpga_multiaction_pose.html"
    existing = existing_path.read_text(encoding="utf-8")
    template = (root / "tools" / "frontend_template.html").read_text(encoding="utf-8")
    manifest = json.loads((root / "tools" / "asset_manifest.json").read_text(encoding="utf-8"))
    asset_names = {"RUNTIME": "vision_bundle.mjs", "LOADER": "vision_wasm_internal.js",
                   "WASM": "vision_wasm_internal.wasm", "MODEL": "pose_landmarker_lite.task"}
    for token, filename in asset_names.items():
        pattern = r'<script\b(?=[^>]*\bid="' + token.lower() + r'")[^>]*>([A-Za-z0-9+/=\s]+)</script>'
        matches = re.findall(pattern, existing)
        if len(matches) != 1:
            raise RuntimeError("找不到唯一内置资源：%s。请恢复原版 HTML 再重建。" % token)
        encoded = "".join(matches[0].split())
        raw = base64.b64decode(encoded, validate=True)
        expected = manifest["assets"][filename]
        if len(raw) != expected["bytes"] or hashlib.sha256(raw).hexdigest() != expected["sha256"]:
            raise RuntimeError("内置资源校验失败：%s" % filename)
        template = template.replace("__%s_B64__" % token, encoded)
    for token, filename in {"ENGINE": "dataset_engine.mjs", "HELPER": "dataset_ui_helpers.mjs",
                            "WORKER": "pose_dataset_worker.mjs", "COACH": "coach_features.mjs",
                            "PROFILE": "pose_profile.mjs", "HOLDPROFILE": "hold_profile.mjs"}.items():
        encoded = base64.b64encode((root / filename).read_bytes()).decode("ascii")
        template = template.replace("__%s_B64__" % token, encoded)
    client = (root / "fpga_pose_client.mjs").read_text(encoding="utf-8")
    template = template.replace("__CLIENT_SOURCE__", client)
    if re.search(r"__[A-Z_]+__", template):
        raise RuntimeError("HTML 模板仍有未替换的资源占位符")
    existing_path.write_text(template, encoding="utf-8")
    print("离线网页已重建，运行库和模型 SHA256 校验通过。")


if __name__ == "__main__":
    main()
