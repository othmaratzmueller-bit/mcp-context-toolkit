import os
import re
from pathlib import Path
import yaml

# Initialize paths relative to workspace root
WORKSPACE_ROOT = Path(__file__).resolve().parents[5] # 5 levels up from mcp_context_toolkit/scripts/migrate_changelogs.py
CHANGELOG_DIRS = [
    WORKSPACE_ROOT / ".claude" / "changelog.d",
    WORKSPACE_ROOT / ".claude" / "changelog.d" / "flushed"
]
DECISIONS_DIR = WORKSPACE_ROOT / ".claude" / "decisions"

def build_file_map(root_dir: Path) -> dict[str, list[str]]:
    """Build a mapping of filename -> list of repo-relative paths."""
    file_map = {}
    # Walk directories that contain code
    for folder in ["backend", "sps_webapp", "docs", ".claude"]:
        dir_path = root_dir / folder
        if not dir_path.is_dir():
            continue
        for p in dir_path.rglob("*"):
            if p.is_file() and not p.name.startswith("."):
                rel_path = str(p.relative_to(root_dir))
                file_map.setdefault(p.name, []).append(rel_path)
    return file_map

def clean_key(title: str) -> str:
    """Generate a clean slug key (must match ^[a-z][a-z0-9_]*$)."""
    # Replace non-alphanumeric with underscores, lower-case
    s = title.strip().lower()
    # Strip suffixes like "— v1.3.891-893" or dates
    s = re.sub(r"—.*$", "", s)
    s = re.sub(r"[—\-\(\)\/\.\,\s]+", "_", s)
    s = re.sub(r"[^a-z0-9_]+", "", s)
    s = s.strip("_")
    # Ensure it starts with a letter
    if not s or not s[0].isalpha():
        s = "dec_" + s
    return s

def parse_changelog_file(file_path: Path, file_map: dict[str, list[str]]) -> list[dict]:
    """Parse frontmatter and roadmap_adr sections from a changelog MD file."""
    content = file_path.read_text(encoding="utf-8")
    
    # 1. Parse frontmatter
    fm_match = re.match(r"^---\s*\n(.*?)\n---\s*\n", content, re.DOTALL)
    if not fm_match:
        return []
    
    fm_data = yaml.safe_load(fm_match.group(1))
    version = fm_data.get("version")
    date_str = str(fm_data.get("date"))
    
    if not version or not date_str:
        return []

    # 2. Extract backticked filenames from the file to match applies_to
    # We find all `filename.ext` in the file.
    filenames = set(re.findall(r"`([a-zA-Z0-9_\-\.\/]+)`", content))
    matched_files = []
    for fn in filenames:
        basename = os.path.basename(fn)
        if basename in file_map:
            matched_files.extend(file_map[basename])
    # Deduplicate and sort
    matched_files = sorted(list(set(matched_files)))

    # 3. Locate ## roadmap_adr section
    adr_sec_match = re.search(r"##\s+roadmap_adr\s*\n(.*?)(?:\n##\s+|$)", content, re.DOTALL | re.IGNORECASE)
    if not adr_sec_match:
        return []
    
    adr_section = adr_sec_match.group(1)
    
    # Find all ADRs in this section (headers start with ###)
    adr_blocks = re.split(r"\n###\s+", "\n" + adr_section)
    decisions = []
    
    for block in adr_blocks:
        block = block.strip()
        if not block:
            continue
        
        lines = block.split("\n")
        title_line = lines[0].strip()
        body = "\n".join(lines[1:]).strip()
        
        # Clean title of suffixes
        clean_title = re.sub(r"\s*—.*$", "", title_line).strip()
        if not clean_title:
            continue
            
        key = clean_key(clean_title)
        
        # Look for Decision, Why, Alternatives rejected patterns
        dec_match = re.search(r"\*\*Decision:\*\*\s*(.*?)(?:\*\*(?:Why|Alternatives rejected):\*\*|$)", body, re.DOTALL | re.IGNORECASE)
        why_match = re.search(r"\*\*Why:\*\*\s*(.*?)(?:\*\*(?:Decision|Alternatives rejected):\*\*|$)", body, re.DOTALL | re.IGNORECASE)
        alt_match = re.search(r"\*\*Alternatives rejected:\*\*\s*(.*?)(?:\*\*(?:Decision|Why):\*\*|$)", body, re.DOTALL | re.IGNORECASE)
        
        if not dec_match:
            # Maybe it is not formatted with **Decision:** but is still a decision
            if len(body) > 10:
                reason = body
                decision_text = clean_title
            else:
                continue
        else:
            decision_text = dec_match.group(1).strip()
            why_text = why_match.group(1).strip() if why_match else ""
            alt_text = alt_match.group(1).strip() if alt_match else ""
            
            # Combine into a rich reason
            parts = []
            if decision_text:
                parts.append(f"Decision: {decision_text}")
            if why_text:
                parts.append(f"Why: {why_text}")
            if alt_text:
                parts.append(f"Alternatives rejected: {alt_text}")
            reason = "\n\n".join(parts)
            
        if len(reason) < 10:
            reason = reason + " (Imported from changelog)"

        # Determine applies_to scope:
        # We can narrow matched files to only those relevant to the key or description if needed,
        # but matching all files in the changelog is generally a great heuristic for "applies_to".
        # If no files matched, it applies globally.
        decisions.append({
            "key": key,
            "title": clean_title,
            "date": date_str,
            "status": "accepted",
            "applies_to": {
                "modules": [],
                "files": matched_files
            },
            "reason": reason
        })
        
    return decisions

def main():
    print(f"Scanning workspace root: {WORKSPACE_ROOT}")
    DECISIONS_DIR.mkdir(parents=True, exist_ok=True)
    
    file_map = build_file_map(WORKSPACE_ROOT)
    print(f"Mapped {len(file_map)} unique files from the codebase.")
    
    total_migrated = 0
    
    for changelog_dir in CHANGELOG_DIRS:
        if not changelog_dir.is_dir():
            continue
        print(f"Processing changelogs in {changelog_dir.relative_to(WORKSPACE_ROOT)}...")
        for p in sorted(changelog_dir.glob("*.md")):
            if p.name == "README.md":
                continue
            try:
                decisions = parse_changelog_file(p, file_map)
                for dec in decisions:
                    # Write separate file
                    filename = f"{dec['date']}_{dec['key']}.yaml"
                    dest_file = DECISIONS_DIR / filename
                    
                    # Merge if already exists (optional, or just overwrite)
                    with open(dest_file, "w", encoding="utf-8") as f:
                        yaml.dump(dec, f, allow_unicode=True, sort_keys=False)
                    
                    total_migrated += 1
            except Exception as e:
                print(f"Error parsing {p.name}: {e}")
                
    print(f"Done! Successfully migrated {total_migrated} architectural decisions into {DECISIONS_DIR.relative_to(WORKSPACE_ROOT)}/.")

if __name__ == "__main__":
    main()
