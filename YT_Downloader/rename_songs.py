import os
import re
from pathlib import Path

target_dir = Path(r"D:\Music\love")

if not target_dir.exists():
    print(f"Directory not found: {target_dir}")
    exit(1)

count = 0
# Matches digits followed by optional spaces, a hyphen, and optional spaces at the start of the string
pattern = re.compile(r"^\d+\s*-\s*")

for file_path in target_dir.iterdir():
    if file_path.is_file():
        new_name = pattern.sub("", file_path.name)
        if new_name != file_path.name:
            new_path = file_path.with_name(new_name)
            if not new_path.exists():
                file_path.rename(new_path)
                print(f"Renamed: '{file_path.name}' -> '{new_name}'")
                count += 1
            else:
                print(f"Skipped: '{new_name}' already exists.")

print(f"\nSuccessfully renamed {count} files!")
