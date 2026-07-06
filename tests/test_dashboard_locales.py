import json
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
LOCALES_DIR = ROOT / "app" / "dashboard" / "public" / "statics" / "locales"
BUILD_LOCALES_DIR = ROOT / "app" / "dashboard" / "build" / "statics" / "locales"
SRC_DIR = ROOT / "app" / "dashboard" / "src"


def _load_locale(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _locale_payloads(locales_dir: Path) -> dict[str, dict]:
    return {
        path.name: _load_locale(path)
        for path in sorted(locales_dir.glob("*.json"))
    }


def test_dashboard_locale_files_have_same_keys():
    locales = _locale_payloads(LOCALES_DIR)
    baseline = set(locales["en.json"])

    assert locales
    for name, payload in locales.items():
        assert set(payload) == baseline, (
            f"{name} locale keys differ from en.json; "
            f"missing={sorted(baseline - set(payload))}; "
            f"extra={sorted(set(payload) - baseline)}"
        )


def test_dashboard_build_locale_files_match_public_locale_files():
    public_locales = _locale_payloads(LOCALES_DIR)
    build_locales = _locale_payloads(BUILD_LOCALES_DIR)

    assert build_locales == public_locales


def test_static_dashboard_translation_keys_exist_in_english_locale():
    english = _load_locale(LOCALES_DIR / "en.json")
    patterns = (
        re.compile(r"\bt\(\s*['\"]([^'\"+]+?)['\"]"),
        re.compile(r"\bi18nKey\s*=\s*['\"]([^'\"]+)['\"]"),
        re.compile(
            r"\b(?:labelKey|descriptionKey|titleKey|messageKey|helperKey|"
            r"placeholderKey|translationKey)\s*:\s*['\"]([^'\"]+)['\"]"
        ),
    )
    ignored_exact_keys = {
        "userDialog.",
        "userDialog.resetStrategy",
    }
    ignored_prefixes = ("status.",)
    ignored_suffixes = (".",)
    missing = {}

    for path in SRC_DIR.rglob("*"):
        if path.suffix not in {".ts", ".tsx", ".js", ".jsx"}:
            continue
        content = path.read_text(encoding="utf-8")
        for pattern in patterns:
            for match in pattern.finditer(content):
                key = match.group(1)
                if key.endswith(ignored_suffixes):
                    continue
                if key in ignored_exact_keys:
                    continue
                if any(key.startswith(prefix) for prefix in ignored_prefixes):
                    continue
                if key not in english:
                    missing.setdefault(key, set()).add(str(path.relative_to(ROOT)))

    assert not missing, {
        key: sorted(paths)
        for key, paths in sorted(missing.items())
    }


def test_dynamic_dashboard_translation_key_families_exist_in_all_locales():
    required_keys = {
        "core.socket.closed",
        "core.socket.connected",
        "core.socket.connecting",
        "core.socket.not_connected",
        "dateInfo.day",
        "dateInfo.hour",
        "dateInfo.min",
        "dateInfo.month",
        "dateInfo.year",
        "nodeModal.status.connected",
        "nodeModal.status.connecting",
        "nodeModal.status.disabled",
        "nodeModal.status.error",
        "status.active",
        "status.disabled",
        "status.expired",
        "status.limited",
        "status.on_hold",
        "userDialog.resetStrategyAnnually",
        "userDialog.resetStrategyDaily",
        "userDialog.resetStrategyMonthly",
        "userDialog.resetStrategyNo",
        "userDialog.resetStrategyWeekly",
    }

    missing = {}
    for name, payload in _locale_payloads(LOCALES_DIR).items():
        missing_keys = required_keys - set(payload)
        if missing_keys:
            missing[name] = sorted(missing_keys)

    assert not missing
