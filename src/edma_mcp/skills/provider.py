from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from .execution import ResolvedSkillExecution, resolve_skill_execution


@dataclass(frozen=True)
class SkillResource:
    skill_id: str
    path: str
    kind: str
    uri: str

    def as_dict(self) -> Dict[str, str]:
        return {
            "skill_id": self.skill_id,
            "path": self.path,
            "kind": self.kind,
            "uri": self.uri,
        }


class FilesystemSkillProvider:
    def __init__(self, skills_root: str):
        self.skills_root = os.path.abspath(skills_root)

    def _skill_dir(self, skill_id: str) -> str:
        return os.path.join(self.skills_root, skill_id)

    def _manifest_path(self, skill_id: str) -> str:
        return os.path.join(self._skill_dir(skill_id), "manifest.json")

    def _content_path(self, skill_id: str) -> str:
        return os.path.join(self._skill_dir(skill_id), "SKILL.md")

    def _classify_resource_kind(self, relative_path: str) -> str:
        normalized = relative_path.replace("\\", "/")
        head = normalized.split("/", 1)[0]
        if head in {"templates", "snippets", "examples", "assets"}:
            return head
        if normalized == "manifest.json":
            return "manifest"
        if normalized == "SKILL.md":
            return "content"
        return "resource"

    def _extract_section(self, text: str, title: str) -> str:
        pattern = rf"^## {re.escape(title)}\s*$([\s\S]*?)(?=^## |\Z)"
        match = re.search(pattern, text, flags=re.MULTILINE)
        return match.group(1).strip() if match else ""

    def _build_fallback_manifest(self, skill_id: str) -> Dict[str, Any]:
        content = self.read_skill_content(skill_id)
        meta: Dict[str, Any] = {
            "id": skill_id,
            "name": skill_id,
            "description": "No description provided.",
            "version": None,
            "kind": "workflow",
            "agents": [],
            "required_tools": [],
            "ordered_tools": [],
            "references": [],
        }
        if content.startswith("---"):
            parts = content.split("---", 2)
            if len(parts) >= 3:
                frontmatter = parts[1]
                for line in frontmatter.splitlines():
                    if ":" not in line:
                        continue
                    key, value = line.split(":", 1)
                    key = key.strip()
                    value = value.strip()
                    if key in {"id", "name", "description", "version"}:
                        meta[key] = value

        agents_text = self._extract_section(content, "Agents Involved")
        meta["agents"] = re.findall(r"^-\\s*([A-Za-z0-9_]+)\\s*$", agents_text, flags=re.MULTILINE)

        required_tools_text = self._extract_section(content, "Required Tools")
        steps_text = self._extract_section(content, "Steps")
        meta["required_tools"] = re.findall(r"`([A-Za-z0-9_]+\\.[A-Za-z0-9_]+)`", required_tools_text)
        meta["ordered_tools"] = re.findall(r"`([A-Za-z0-9_]+\\.[A-Za-z0-9_]+)`", steps_text)
        refs = re.findall(r"`([A-Za-z0-9_]+)`", steps_text + "\n" + self._extract_section(content, "Planning Contract"))
        meta["references"] = [
            ref for ref in refs
            if ref != skill_id and "." not in ref and ref not in {"done", "stop", "ask_user"}
        ]
        if "compound skill" in content.lower():
            meta["kind"] = "compound_workflow"
        return meta

    def _resource_uri(self, skill_id: str, relative_path: str) -> str:
        normalized = relative_path.replace("\\", "/")
        return f"skill://{skill_id}/{normalized}"

    def list_skills(self) -> List[Dict[str, Any]]:
        if not os.path.isdir(self.skills_root):
            return []
        skills: List[Dict[str, Any]] = []
        for entry in sorted(os.listdir(self.skills_root)):
            skill_dir = os.path.join(self.skills_root, entry)
            if not os.path.isdir(skill_dir):
                continue
            manifest_path = os.path.join(skill_dir, "manifest.json")
            content_path = os.path.join(skill_dir, "SKILL.md")
            if not os.path.isfile(manifest_path) and not os.path.isfile(content_path):
                continue
            meta = {
                "id": entry,
                "name": entry,
                "description": "No description provided.",
                "version": None,
                "kind": "workflow",
            }
            if os.path.isfile(manifest_path):
                try:
                    with open(manifest_path, "r", encoding="utf-8") as f:
                        manifest = json.load(f)
                    if isinstance(manifest, dict):
                        meta.update({
                            "id": manifest.get("id", entry),
                            "name": manifest.get("name", meta["name"]),
                            "description": manifest.get("description", meta["description"]),
                            "version": manifest.get("version"),
                            "kind": manifest.get("kind", meta["kind"]),
                        })
                except Exception:
                    pass
            skills.append(meta)
        return skills

    def get_skill_manifest(self, skill_id: str) -> Dict[str, Any]:
        manifest_path = self._manifest_path(skill_id)
        if os.path.isfile(manifest_path):
            with open(manifest_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                raise ValueError(f"Skill manifest for '{skill_id}' must be a JSON object.")
            return data
        if os.path.isfile(self._content_path(skill_id)):
            return self._build_fallback_manifest(skill_id)
        raise FileNotFoundError(f"Skill manifest not found for '{skill_id}'.")

    def resolve_skill_execution(self, skill_id: str) -> Dict[str, str]:
        manifest = self.get_skill_manifest(skill_id)
        resolved: ResolvedSkillExecution = resolve_skill_execution(manifest)
        return resolved.as_dict()

    def read_skill_content(self, skill_id: str) -> str:
        content_path = self._content_path(skill_id)
        if not os.path.isfile(content_path):
            raise FileNotFoundError(f"Skill content not found for '{skill_id}'.")
        with open(content_path, "r", encoding="utf-8") as f:
            return f.read()

    def list_skill_resources(self, skill_id: str) -> List[Dict[str, str]]:
        skill_dir = self._skill_dir(skill_id)
        if not os.path.isdir(skill_dir):
            raise FileNotFoundError(f"Skill '{skill_id}' not found.")
        resources: List[SkillResource] = []
        for root, _, files in os.walk(skill_dir):
            for filename in sorted(files):
                abs_path = os.path.join(root, filename)
                rel_path = os.path.relpath(abs_path, skill_dir)
                kind = self._classify_resource_kind(rel_path)
                resources.append(
                    SkillResource(
                        skill_id=skill_id,
                        path=rel_path.replace("\\", "/"),
                        kind=kind,
                        uri=self._resource_uri(skill_id, rel_path),
                    )
                )
        return [resource.as_dict() for resource in sorted(resources, key=lambda item: item.path)]

    def read_skill_resource(self, skill_id: str, resource_path: str) -> str:
        normalized = resource_path.replace("\\", "/").lstrip("/")
        abs_path = os.path.abspath(os.path.join(self._skill_dir(skill_id), normalized))
        skill_dir = os.path.abspath(self._skill_dir(skill_id))
        if not abs_path.startswith(skill_dir + os.sep) and abs_path != skill_dir:
            raise ValueError("Resource path escapes the skill directory.")
        if not os.path.isfile(abs_path):
            raise FileNotFoundError(f"Resource '{resource_path}' not found for skill '{skill_id}'.")
        with open(abs_path, "r", encoding="utf-8") as f:
            return f.read()

    def build_skills_catalog(self) -> str:
        skills = self.list_skills()
        catalog = "AVAILABLE SKILLS CATALOG:\n\n"
        for skill in skills:
            catalog += f"- **ID**: {skill.get('id')}\n"
            catalog += f"  **Name**: {skill.get('name')}\n"
            catalog += f"  **Description**: {skill.get('description')}\n"
            if skill.get("kind"):
                catalog += f"  **Kind**: {skill.get('kind')}\n"
            if skill.get("version"):
                catalog += f"  **Version**: {skill.get('version')}\n"
            catalog += "\n"
        return catalog
