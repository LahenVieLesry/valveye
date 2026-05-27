"""Versioned prompt template system.

Loads prompts from YAML files in src/valveye/prompts/ with version tracking.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


@dataclass(frozen=True)
class PromptVersion:
    version: str
    template: str
    changelog: str
    variables: list[str]


class PromptManager:
    """Manages versioned prompt templates loaded from YAML files."""

    def __init__(self, prompts_dir: Path | None = None) -> None:
        self._prompts: dict[str, dict[str, PromptVersion]] = {}
        self._active_versions: dict[str, str] = {}
        self._load(prompts_dir or Path(__file__).parent / "prompts")

    def _load(self, prompts_dir: Path) -> None:
        if not prompts_dir.exists():
            return

        for yaml_file in sorted(prompts_dir.glob("*.yaml")):
            try:
                data = yaml.safe_load(yaml_file.read_text(encoding="utf-8"))
                if not isinstance(data, dict):
                    continue

                name = data.get("name", yaml_file.stem)
                active = data.get("active", "latest")
                versions_data = data.get("versions", {})

                versions: dict[str, PromptVersion] = {}
                for ver, ver_data in versions_data.items():
                    if isinstance(ver_data, dict):
                        versions[ver] = PromptVersion(
                            version=ver,
                            template=ver_data.get("template", ""),
                            changelog=ver_data.get("changelog", ""),
                            variables=ver_data.get("variables", []),
                        )

                if versions:
                    self._prompts[name] = versions
                    # Set active version (default to latest available)
                    if active == "latest":
                        self._active_versions[name] = max(versions.keys())
                    elif active in versions:
                        self._active_versions[name] = active
                    else:
                        self._active_versions[name] = max(versions.keys())
            except Exception:
                continue

    def get(self, name: str, **kwargs: Any) -> str:
        """Render the active version of a prompt with given variables."""
        if name not in self._prompts:
            raise KeyError(f"Prompt '{name}' not found")

        version_key = self._active_versions.get(name)
        if not version_key or version_key not in self._prompts[name]:
            raise KeyError(f"Active version '{version_key}' not found for prompt '{name}'")

        prompt = self._prompts[name][version_key]
        if kwargs:
            return prompt.template.format(**kwargs)
        return prompt.template

    def list_prompts(self) -> list[str]:
        """List all available prompt names."""
        return list(self._prompts.keys())

    def list_versions(self, name: str) -> list[str]:
        """List all versions for a given prompt."""
        if name not in self._prompts:
            return []
        return list(self._prompts[name].keys())

    def get_version(self, name: str, version: str) -> PromptVersion | None:
        """Get a specific version of a prompt."""
        return self._prompts.get(name, {}).get(version)

    def set_version(self, name: str, version: str) -> None:
        """Set the active version for a prompt."""
        if name not in self._prompts:
            raise KeyError(f"Prompt '{name}' not found")
        if version not in self._prompts[name]:
            raise KeyError(f"Version '{version}' not found for prompt '{name}'")
        self._active_versions[name] = version

    @property
    def active_versions(self) -> dict[str, str]:
        """Get all active versions."""
        return dict(self._active_versions)


# Module-level singleton
_prompt_manager: PromptManager | None = None


def get_prompt_manager() -> PromptManager:
    """Get or create the singleton PromptManager."""
    global _prompt_manager
    if _prompt_manager is None:
        _prompt_manager = PromptManager()
    return _prompt_manager
