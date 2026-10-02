"""What the plugin declares (plugin.yaml) must match what it registers."""

from pathlib import Path

from ruamel.yaml import YAML  # ships with Hermes

import adapter as cw


def _load(path):
    return YAML(typ="safe").load(path.read_text(encoding="utf-8"))

ROOT = Path(__file__).resolve().parents[1]


class RecordingCtx:
    def __init__(self):
        self.platforms, self.tools, self.hooks, self.middleware = [], [], [], []

    def register_platform(self, **kw):
        self.platforms.append(kw)

    def register_tool(self, *a, **kw):
        self.tools.append(kw.get("name") or a[0])

    def register_hook(self, name, cb):
        self.hooks.append(name)

    def register_middleware(self, kind, cb):
        self.middleware.append(kind)


def _registered():
    import importlib.util
    spec = importlib.util.spec_from_file_location("chatwork_plugin", ROOT / "__init__.py",
                                                  submodule_search_locations=[str(ROOT)])
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    ctx = RecordingCtx()
    mod.register(ctx)
    return ctx


def _env_names(entries):
    return [e if isinstance(e, str) else e["name"] for e in entries or []]


def test_registers_one_chatwork_platform_and_nothing_else():
    ctx = _registered()
    assert [p["name"] for p in ctx.platforms] == ["chatwork"]
    assert ctx.tools == [] and ctx.hooks == [] and ctx.middleware == []
    entry = ctx.platforms[0]
    assert entry["allowed_users_env"] == "CHATWORK_ALLOWED_USERS"
    assert entry["cron_deliver_env_var"] == "CHATWORK_HOME_CHANNEL"
    assert entry["max_message_length"] == cw.MAX_MESSAGE_LENGTH
    assert callable(entry["standalone_sender_fn"])


def test_manifest_matches_registration():
    manifest = _load(ROOT / "plugin.yaml")
    ctx = _registered()
    assert manifest["name"] == "jp-chatwork" == cw.PLUGIN_NAME  # catalog key; also the plugin-data folder
    assert manifest["kind"] == "platform" and ctx.platforms[0]["name"] == cw.PLATFORM_NAME == "chatwork"
    assert list(manifest.get("provides_tools") or []) == ctx.tools == []
    assert list(manifest.get("provides_hooks") or []) == ctx.hooks == []
    assert ctx.middleware == []
    assert _env_names(manifest["requires_env"]) == ctx.platforms[0]["required_env"]
    assert manifest["manifest_version"] == 2 and manifest["requires_hermes"] == ">=0.21.4"
    assert "Unofficial" in manifest["description"] and "not affiliated" in manifest["description"]
    assert not (ROOT / "catalog-entry.yaml").exists()  # the catalog entry lives in the Hermes repo


def test_every_env_var_the_code_reads_is_documented():
    manifest = _load(ROOT / "plugin.yaml")
    documented = set(_env_names(manifest["requires_env"])) | set(_env_names(manifest.get("optional_env")))
    source = "".join(p.read_text(encoding="utf-8") for p in ROOT.glob("*.py"))
    import re
    used = set(re.findall(r'"(CHATWORK_[A-Z_]+)"', source))
    # CHATWORK_ALLOW_ALL_USERS and CHATWORK_HOME_CHANNEL_NAME are Hermes-wide conventions handled by the gateway.
    assert used - documented <= {"CHATWORK_ALLOW_ALL_USERS", "CHATWORK_HOME_CHANNEL_NAME"}
