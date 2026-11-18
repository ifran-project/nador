"""Profile loading and preflight validation for monitoring deployments."""

import re
from pathlib import Path

import yaml
from ifran.utils.utils import deep_merge
from pydantic import ValidationError

from nador.models import MonitoringDeployment, component_image_defaults


class ProfileLoader(yaml.SafeLoader):
    """Reject duplicate options instead of silently choosing the last value."""

    def construct_mapping(self, node, deep=False):
        """Build a mapping, rejecting duplicate keys."""
        mapping = super().construct_mapping(node, deep=deep)
        keys = [self.construct_object(key, deep=deep) for key, _ in node.value]
        if len(keys) != len(mapping):
            raise yaml.constructor.ConstructorError(
                None,
                None,
                "duplicate monitoring option",
                node.start_mark,
            )
        return mapping


ProfileLoader.yaml_implicit_resolvers = {
    ch: [(tag, pattern) for tag, pattern in entries if tag != "tag:yaml.org,2002:bool"]
    for ch, entries in yaml.SafeLoader.yaml_implicit_resolvers.items()
}
ProfileLoader.add_implicit_resolver("tag:yaml.org,2002:bool", re.compile("^(true|false)$"), list("tf"))


def switches(defaults=None, prefix=()):
    """Yield explicit switch paths, including switches introduced by new model fields."""
    if defaults is None:
        defaults = MonitoringDeployment().model_dump(mode="json", exclude={"components", "images", "targets"})
    for key, value in defaults.items():
        path = (*prefix, key)
        if type(value) is bool:
            yield path
        elif isinstance(value, dict):
            yield from switches(value, path)


def load_profile(path):
    """Read a profile and fill partial component image overrides."""
    data = yaml.load(path.read_text(), Loader=ProfileLoader)  # noqa: S506 - ProfileLoader is a SafeLoader
    if not isinstance(data, dict):
        raise ValueError("expected a YAML mapping")
    components = data.get("components")
    if isinstance(components, dict):
        for name, override in components.items():
            image = override.get("image") if isinstance(override, dict) else None
            if isinstance(image, dict) and (default_image := component_image_defaults(name)):
                override["image"] = deep_merge(default_image.copy(), image)
    return data


def validate_deployment[M: MonitoringDeployment](
    data: dict,
    model: type[M] = MonitoringDeployment,
    *,
    source: str = "Monitoring configuration",
    explicit: bool = False,
) -> M:
    """Validate deployment values and report field errors without displaying input values."""
    errors = []
    if explicit:
        for keys in switches():
            value = data
            for key in keys:
                value = value.get(key) if isinstance(value, dict) else None
            if type(value) is not bool:
                errors.append(f"{'.'.join(keys)}: write an explicit true or false")
    try:
        config = model.model_validate(data)
    except ValidationError as exc:
        errors.extend(f"{'.'.join(map(str, error['loc'])) or 'profile'}: {error['msg']}" for error in exc.errors())
        raise ValueError(_report(source, errors)) from None
    if errors:
        raise ValueError(_report(source, errors))
    return config


def _report(source: str, errors: list[str]) -> str:
    return f"{source}:\n  - " + "\n  - ".join(errors)


def read_deployment[M: MonitoringDeployment](path: str | Path, model: type[M] = MonitoringDeployment) -> M:
    """Load a full monitoring profile before planning or deploying it."""
    path = Path(path)
    try:
        data = load_profile(path)
    except (OSError, ValueError, yaml.YAMLError) as exc:
        raise ValueError(f"Invalid monitoring profile {path}: {exc}") from exc
    return validate_deployment(data, model, source=f"Invalid monitoring profile {path}", explicit=True)
