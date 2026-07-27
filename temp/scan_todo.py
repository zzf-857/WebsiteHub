import os
import re
from pathlib import Path

ROOT = Path("F:/AI/AgentMake/AgentProjects/WebHub").resolve()

TARGET_DIRS = [
    ROOT / "services" / "api" / "src",
    ROOT / "apps" / "web",
]

PATTERN = re.compile(r"(TODO|FIXME|NotImplementedError|raise NotImplemented)", re.IGNORECASE)

def scan():
    results = []
    for target in TARGET_DIRS:
        for root, dirs, files in os.walk(target):
            # Skip node_modules and .next
            dirs[:] = [d for d in dirs if d not in {"node_modules", ".next", "__pycache__"}]
            for f in files:
                if f.endswith((".py", ".ts", ".tsx", ".js", ".jsx")):
                    file_path = Path(root) / f
                    try:
                        with open(file_path, "r", encoding="utf-8", errors="ignore") as file:
                            for i, line in enumerate(file, 1):
                                if PATTERN.search(line):
                                    results.append(f"{file_path.relative_to(ROOT)}:{i} -> {line.strip()}")
                    except Exception as e:
                        pass
    return results

if __name__ == "__main__":
    findings = scan()
    print(f"Found {len(findings)} matches:")
    for item in findings:
        print(item)
