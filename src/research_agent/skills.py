from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

from .domain import digest


@dataclass(frozen=True)
class Skill:
    name: str
    description: str
    tools: tuple[str, ...]
    body: str
    path: Path
    revision: str


class SkillRegistry:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.skills: dict[str, Skill] = {}
        for path in sorted(self.root.glob("*/SKILL.md")):
            if path.is_symlink() or not path.resolve().is_relative_to(self.root):
                raise ValueError("Skill paths must stay inside the curated skill root")
            raw = path.read_text()
            parts = raw.split("---", 2)
            if len(parts) != 3 or parts[0].strip():
                raise ValueError(f"Invalid skill frontmatter: {path.name}")
            metadata = yaml.safe_load(parts[1])
            name = metadata["name"]
            if name != path.parent.name or name in self.skills:
                raise ValueError("Skill name/directory mismatch or collision")
            self.skills[name] = Skill(name, metadata["description"], tuple(str(metadata.get("allowed-tools", "")).split()),
                                      parts[2].strip(), path, digest(raw))

    def catalog(self) -> list[dict]:
        return [{"name": s.name, "description": s.description, "tools": s.tools, "revision": s.revision}
                for s in self.skills.values()]

    def get(self, name: str) -> Skill:
        if name not in self.skills:
            raise ValueError("Unknown skill")
        return self.skills[name]

    def reference(self, name: str, relative_path: str) -> str:
        skill = self.get(name)
        target = (skill.path.parent / relative_path).resolve()
        if not target.is_relative_to(skill.path.parent.resolve()) or target.suffix != ".md":
            raise ValueError("Reference must be a Markdown file inside its skill")
        if target.is_symlink() or not target.is_file() or target.stat().st_size > 16000:
            raise ValueError("Reference unavailable or too large")
        return target.read_text()

    def allowed(self, action: str, loaded: list[str]) -> bool:
        return any(action in self.get(name).tools for name in loaded)
