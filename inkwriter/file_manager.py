"""
File management: creating, listing, saving, loading documents.
All files are plain .txt stored on the SD card under ~/Documents/inkwriter/

Bug fixes vs original:
  - list_files(directory) now lists only files directly in `directory`
    (not recursive), so the file browser shows one directory at a time.
    The type-out helper still passes a directory=None which lists
    everything recursively via list_all_files().
  - Notes directory is excluded from document listings.
"""

import os
import shutil
import time
import re
from pathlib import Path
from datetime import datetime


class FileManager:
    def __init__(self, config):
        self.config = config

    # ------------------------------------------------------------------
    # Listing
    # ------------------------------------------------------------------

    def list_files(self, directory=None):
        """
        Return list of Path for .txt files directly inside `directory`,
        sorted newest-modified first.

        Notes are excluded (files under config.notes_dir).
        """
        base = directory or self.config.documents_dir
        notes = self.config.notes_dir
        files = []
        try:
            for p in base.iterdir():
                if (
                    p.is_file()
                    and p.suffix == ".txt"
                    and ".backup" not in p.parts
                    and not _is_under(p, notes)
                ):
                    files.append(p)
        except PermissionError:
            pass
        files.sort(key=lambda x: x.stat().st_mtime, reverse=True)
        return files

    def list_all_files(self):
        """
        Recursively list every .txt document (for type-out screen).
        Notes and backups excluded.
        """
        base = self.config.documents_dir
        notes = self.config.notes_dir
        files = []
        for p in sorted(base.rglob("*.txt"), key=lambda x: x.stat().st_mtime, reverse=True):
            if ".backup" not in p.parts and not _is_under(p, notes):
                files.append(p)
        return files

    def list_directories(self, parent=None):
        """Return subdirectories of parent (default: documents root)."""
        base = parent or self.config.documents_dir
        dirs = []
        try:
            dirs = [d for d in sorted(base.iterdir()) if d.is_dir() and d.name not in (".backup", "notes")]
        except PermissionError:
            pass
        return dirs

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------

    def create_new_file(self, directory=None, title=None):
        """Create a new empty file. Returns the Path."""
        base = directory or self.config.documents_dir
        if title:
            filename = self._safe_filename(title) + ".txt"
        else:
            filename = datetime.now().strftime("doc_%Y%m%d_%H%M%S.txt")
        path = base / filename
        counter = 1
        while path.exists():
            stem = path.stem + f"_{counter}"
            path = base / (stem + ".txt")
            counter += 1
        path.touch()
        return path

    def create_directory(self, name, parent=None):
        """Create a new subdirectory."""
        base = parent or self.config.documents_dir
        new_dir = base / self._safe_filename(name)
        new_dir.mkdir(parents=True, exist_ok=True)
        return new_dir

    def load_file(self, path):
        """Return text content of file, or empty string if missing."""
        path = Path(path)
        if not path.exists():
            return ""
        return path.read_text(encoding="utf-8", errors="replace")

    def save_file(self, path, content):
        """Save content to path, optionally backing up first."""
        path = Path(path)
        if self.config.backup_on_save and path.exists():
            self._backup(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    def move_file(self, path, dest_dir):
        """Move a file into dest_dir. Returns the new Path. Avoids clobbering
        an existing file of the same name by appending a numeric suffix."""
        path = Path(path)
        dest_dir = Path(dest_dir)
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / path.name
        counter = 1
        while dest.exists():
            dest = dest_dir / f"{path.stem}_{counter}{path.suffix}"
            counter += 1
        shutil.move(str(path), str(dest))
        return dest

    def rename_file(self, path, new_name):
        """Rename a file. Returns new Path."""
        path = Path(path)
        new_name = self._safe_filename(new_name)
        new_path = path.parent / (new_name + ".txt")
        path.rename(new_path)
        return new_path

    def delete_file(self, path):
        """Move file to .backup/trash/ instead of permanently deleting."""
        path = Path(path)
        trash = self.config.documents_dir / ".backup" / "trash"
        trash.mkdir(parents=True, exist_ok=True)
        dest = trash / f"{path.stem}_{int(time.time())}.txt"
        shutil.move(str(path), str(dest))

    # ------------------------------------------------------------------
    # Metrics
    # ------------------------------------------------------------------

    def word_count(self, content):
        return len(content.split())

    def char_count(self, content):
        return len(content)

    def file_info(self, path):
        """Return dict with metadata about a file."""
        path = Path(path)
        stat = path.stat()
        content = self.load_file(path)
        return {
            "name": path.stem,
            "path": path,
            "size": stat.st_size,
            "modified": datetime.fromtimestamp(stat.st_mtime),
            "words": self.word_count(content),
            "chars": self.char_count(content),
        }

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _backup(self, path):
        backup_dir = self.config.documents_dir / ".backup"
        backup_dir.mkdir(parents=True, exist_ok=True)
        ts = int(time.time())
        dest = backup_dir / f"{path.stem}_{ts}.txt"
        shutil.copy2(str(path), str(dest))
        # Keep only last 5 backups per file stem
        backups = sorted(backup_dir.glob(f"{path.stem}_*.txt"))
        for old in backups[:-5]:
            old.unlink()

    def _safe_filename(self, name):
        name = name.strip()
        name = re.sub(r'[^\w\s\-]', '', name)
        name = re.sub(r'\s+', '_', name)
        return name[:64] or "untitled"


def _is_under(path, parent):
    """Return True if path is inside parent directory."""
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False
