"""Conservative platform applicability hints from captured paths and text only.

This is not a YAML parser, template renderer, schema validator, or deployment
detector. False negatives are possible for custom resources and complex YAML.
No path is opened, imported, executed, or used to retrieve additional context.
"""

import json
import re
from dataclasses import dataclass
from pathlib import PurePosixPath

from .local_source import CollectedSnapshot


_DOCUMENT = frozenset({".yaml", ".yml", ".json"})
_GITOPS_DIRS = frozenset({"gitops", "flux", "k8s", "kubernetes", "helm"})
_KUSTOMIZATION = frozenset({"kustomization", "kustomization.yaml", "kustomization.yml"})
_CHART = frozenset({"chart.yaml", "chart.yml"})
_BUILD_FILES = frozenset({"pom.xml", "build.gradle", "build.gradle.kts", "settings.gradle", "package.json", "dockerfile"})
_RESOURCE_KINDS = {
    "": frozenset({"Pod", "Service", "ConfigMap", "Secret", "Namespace", "ServiceAccount", "PersistentVolume", "PersistentVolumeClaim", "ReplicationController"}),
    "apps": frozenset({"Deployment", "StatefulSet", "DaemonSet", "ReplicaSet"}),
    "batch": frozenset({"Job", "CronJob"}),
    "networking.k8s.io": frozenset({"Ingress", "IngressClass", "NetworkPolicy"}),
    "rbac.authorization.k8s.io": frozenset({"Role", "RoleBinding", "ClusterRole", "ClusterRoleBinding"}),
    "policy": frozenset({"PodDisruptionBudget"}),
    "source.toolkit.fluxcd.io": frozenset({"GitRepository", "HelmRepository", "HelmChart", "OCIRepository", "Bucket"}),
    "helm.toolkit.fluxcd.io": frozenset({"HelmRelease"}),
    "kustomize.toolkit.fluxcd.io": frozenset({"Kustomization"}),
    "notification.toolkit.fluxcd.io": frozenset({"Alert", "Provider", "Receiver"}),
    "image.toolkit.fluxcd.io": frozenset({"ImageRepository", "ImagePolicy", "ImageUpdateAutomation"}),
    "kustomize.config.k8s.io": frozenset({"Kustomization", "Component"}),
}
_SCALAR = re.compile(r"[A-Za-z0-9./_-]+\Z")
_VERSION = re.compile(r"v[0-9]+(?:(?:alpha|beta)[0-9]+)?\Z")


def _resource_hint(fields: dict) -> bool:
    api, kind = fields.get("apiVersion"), fields.get("kind")
    if not isinstance(api, str) or not isinstance(kind, str):
        return False
    group, _, version = api.rpartition("/")
    return bool(_VERSION.fullmatch(version)) and kind in _RESOURCE_KINDS.get(group, ())


def _document_hint(text: str, suffix: str) -> bool:
    if suffix == ".json":
        try:
            value = json.loads(text)
        except (ValueError, RecursionError):
            return False
        return isinstance(value, dict) and _resource_hint(value)
    # Recognize only simple literal top-level pairs in the SAME document.
    # Indented examples, comments, aliases, tags and templated values cannot
    # establish a resource hint. Duplicate relevant keys invalidate a document.
    fields, duplicate = {}, False
    for line in [*text.splitlines(), "---"]:
        if re.fullmatch(r"(?:---|\.\.\.)(?:\s+#.*)?\s*", line):
            if not duplicate and _resource_hint(fields):
                return True
            fields, duplicate = {}, False
            continue
        match = re.match(r"^(apiVersion|kind):[ \t]+(.*)$", line)
        if not match:
            continue
        key, value = match.groups()
        if key in fields:
            duplicate = True
        value = re.split(r"\s+#", value, maxsplit=1)[0].strip()
        if len(value) >= 2 and value[0] in "\"'" and value[-1] == value[0]:
            value = value[1:-1]
        fields[key] = value if _SCALAR.fullmatch(value) else None
    return False


def _jenkinsfile(path: PurePosixPath) -> bool:
    return path.name == "jenkinsfile" or path.name.startswith("jenkinsfile.")


@dataclass(frozen=True)
class PlatformApplicability:
    gitops_files: frozenset[str]
    cicd_files: frozenset[str]
    uncertain_files: tuple[str, ...]
    limitations: tuple[str, ...]


def platform_applicability(snapshot: CollectedSnapshot) -> PlatformApplicability:
    paths = {entry.file_path: PurePosixPath(entry.file_path.lower()) for entry in snapshot.manifest.files}
    chart_roots = {path.parent for path in paths.values() if path.name in _CHART}
    has_jenkinsfile = any(_jenkinsfile(path) for path in paths.values())
    gitops, cicd, uncertain, limitations = set(), set(), set(), []
    for original, path in sorted(paths.items()):
        directory_hint = bool(set(path.parts[:-1]) & _GITOPS_DIRS)
        chart_hint = any(root == path.parent or root in path.parents for root in chart_roots)
        document = path.suffix in _DOCUMENT
        if (path.name in _KUSTOMIZATION | _CHART
                or (document or path.suffix == ".tpl") and (directory_hint or chart_hint)
                or document and any(_document_hint(text, path.suffix)
                                    for (_, source_path), text in snapshot.sources.items() if source_path == original)):
            gitops.add(original)
        if (_jenkinsfile(path) or path.suffix == ".groovy"
                or document and bool(set(path.parts[:-1]) & {"jenkins", ".jenkins"})
                or has_jenkinsfile and path.name in _BUILD_FILES):
            cicd.add(original)
        if document and original not in gitops | cicd:
            uncertain.add(original)
            limitations.append("Some YAML/JSON files lack recognized platform indicators; GitOps/Jenkins applicability is uncertain. Security routing still applies.")
        if path.suffix == ".groovy":
            uncertain.add(original)
            limitations.append("Groovy source is selected for static review; Jenkins Shared Library membership and runtime context remain unverified, including vars/ and src/ conventions.")
    return PlatformApplicability(frozenset(gitops), frozenset(cicd), tuple(sorted(uncertain)), tuple(dict.fromkeys(limitations)))
