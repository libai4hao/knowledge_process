import hashlib
import logging
import shutil
from pathlib import Path
from typing import Dict


class AssetManager:
    """处理图片迁移、去重与命名"""
    def __init__(self, assets_dir: Path, dry_run: bool = False):
        self.assets_dir = assets_dir
        self.dry_run = dry_run
        self.hash_to_path: Dict[str, Path] = {}  # sha256 -> 存储路径
        self.logger = logging.getLogger("knowledge_processor")

    def _get_file_hash(self, path: Path) -> str:
        sha256 = hashlib.sha256()
        with path.open("rb") as f:
            while chunk := f.read(8192):
                sha256.update(chunk)
        return sha256.hexdigest()

    def migrate_file(self, src_path: Path) -> str:
        """根据 Hash 迁移文件，返回相对路径"""
        if not src_path.exists():
            return str(src_path)

        f_hash = self._get_file_hash(src_path)
        
        # 如果该 Hash 已存在，直接复用
        if f_hash in self.hash_to_path:
            return f"assets/{self.hash_to_path[f_hash].name}"

        # 否则，复制文件
        target_name = src_path.name
        dest = self.assets_dir / target_name
        
        # 处理同名但内容不同的冲突
        counter = 1
        while dest.exists():
            if self._get_file_hash(dest) == f_hash:
                break
            dest = self.assets_dir / f"{src_path.stem}_{counter}{src_path.suffix}"
            counter += 1

        if not self.dry_run:
            self.assets_dir.mkdir(parents=True, exist_ok=True)
            if not dest.exists():
                shutil.copy2(src_path, dest)
        
        self.hash_to_path[f_hash] = dest
        return f"assets/{dest.name}"