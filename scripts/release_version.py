"""读取权威包版本并核对公开版本号，供 Windows 发布入口使用。"""

import tomllib
from pathlib import Path

from genesis_sandbox_client import __version__


def main() -> None:
    metadata_path = Path(__file__).resolve().parents[1] / "pyproject.toml"
    metadata = tomllib.loads(metadata_path.read_text(encoding="utf-8"))
    version = metadata["project"]["version"]
    if version != __version__:
        raise SystemExit(f"SDK version mismatch: metadata={version}, public={__version__}")
    print(version)


if __name__ == "__main__":
    main()
