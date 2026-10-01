"""Outside plugins: a private overlay adds integrations to an
unmodified Voxwire, either as an installed package that declares them in the
`voxwire.integrations` entry-point group, or as a folder of plugin modules
named in VOXWIRE_PLUGIN_PATH. The conftest fixture unregisters whatever a test
adds, and each test uses its own module names so imports can't leak between them.
"""
import os
import textwrap

import gateway

PLUGIN = textwrap.dedent('''
    from integrations import Integration, Result, register


    class {cls}(Integration):
        name = "{name}"
        wake_words = ("{word}",)

        def handle(self, command):
            return Result(ok=True, reply="{name} heard: " + command.text)


    register({cls}())
''')


def _fake_distribution(site, module, entries):
    """Make `site` look like it holds an installed package with entry points."""
    dist = site / f"{module}-0.1.dist-info"
    dist.mkdir()
    (dist / "METADATA").write_text(f"Metadata-Version: 2.1\nName: {module}\nVersion: 0.1\n")
    lines = "".join(f"{k} = {v}\n" for k, v in entries.items())
    (dist / "entry_points.txt").write_text(f"[voxwire.integrations]\n{lines}")


# ── VOXWIRE_PLUGIN_PATH ────────────────────────────────────────────────────
def test_plugin_path_loads_a_folder_of_plugins(tmp_path, monkeypatch):
    (tmp_path / "overlay_hello.py").write_text(
        PLUGIN.format(cls="Hello", name="overlay-hello", word="bonjour"))
    (tmp_path / "_helper.py").write_text("raise RuntimeError('underscore files are not plugins')")
    monkeypatch.setenv("VOXWIRE_PLUGIN_PATH", str(tmp_path))
    assert "overlay-hello" in gateway.load_integrations(force=True)
    out = gateway.dispatch("bonjour tout le monde")
    assert out["handled_by"] == "overlay-hello" and out["ok"]
    assert gateway.load_errors() == {}


def test_plugin_path_takes_several_folders_and_skips_missing_ones(tmp_path, monkeypatch):
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    (a / "p_one.py").write_text(PLUGIN.format(cls="One", name="overlay-one", word="uno"))
    (b / "p_two.py").write_text(PLUGIN.format(cls="Two", name="overlay-two", word="dos"))
    monkeypatch.setenv("VOXWIRE_PLUGIN_PATH",
                       os.pathsep.join([str(a), "", str(tmp_path / "missing"), str(b)]))
    assert {"overlay-one", "overlay-two"} <= set(gateway.load_integrations(force=True))


def test_a_broken_outside_plugin_is_reported_and_skipped(tmp_path, monkeypatch):
    (tmp_path / "zz_broken.py").write_text("raise ImportError('needs slack_sdk')")
    (tmp_path / "fine_plugin.py").write_text(PLUGIN.format(cls="Fine", name="overlay-fine", word="okay"))
    monkeypatch.setenv("VOXWIRE_PLUGIN_PATH", str(tmp_path))
    names = gateway.load_integrations(force=True)
    assert {"overlay-fine", "echo"} <= set(names)
    errors = gateway.load_errors()
    assert any("zz_broken" in where and "needs slack_sdk" in why for where, why in errors.items())


# ── entry points ───────────────────────────────────────────────────────────
def test_an_installed_package_can_declare_a_plugin_module(tmp_path, monkeypatch):
    (tmp_path / "overlay_pkg_mod.py").write_text(
        PLUGIN.format(cls="Mod", name="overlay-mod", word="hola"))
    _fake_distribution(tmp_path, "overlay_pkg_mod", {"mod": "overlay_pkg_mod"})
    monkeypatch.syspath_prepend(str(tmp_path))
    assert "overlay-mod" in gateway.load_integrations(force=True)


def test_an_installed_package_can_declare_an_instance_or_a_class(tmp_path, monkeypatch):
    (tmp_path / "overlay_pkg_obj.py").write_text(textwrap.dedent('''
        from integrations import Integration, Result


        class Obj(Integration):
            name = "overlay-obj"
            wake_words = ("ciao",)

            def handle(self, command):
                return Result(ok=True, reply="ciao")


        class Cls(Obj):
            name = "overlay-cls"


        plugin = Obj()
    '''))
    _fake_distribution(tmp_path, "overlay_pkg_obj",
                       {"obj": "overlay_pkg_obj:plugin", "cls": "overlay_pkg_obj:Cls"})
    monkeypatch.syspath_prepend(str(tmp_path))
    assert {"overlay-obj", "overlay-cls"} <= set(gateway.load_integrations(force=True))


def test_the_integrations_endpoint_reports_load_errors_without_their_detail(tmp_path, monkeypatch):
    """An overlay's import error can carry a token or an internal host name. Over
    HTTP only the plugin and the exception type go out; the detail stays local."""
    import pytest
    server = pytest.importorskip("server", reason="server.py needs the audio stack")
    from fastapi.testclient import TestClient
    (tmp_path / "zz_endpoint_broken.py").write_text(
        "raise ImportError('token=sk-not-a-real-secret on build-host.corp.invalid')")
    monkeypatch.setenv("VOXWIRE_PLUGIN_PATH", str(tmp_path))
    gateway.load_integrations(force=True)
    resp = TestClient(server.app).get("/api/integrations")
    body = resp.json()
    assert any(i["name"] == "echo" for i in body["integrations"])
    assert body["errors"] == {"zz_endpoint_broken.py": "ImportError"}
    for leaked in ("sk-not-a-real-secret", "corp.invalid", str(tmp_path)):
        assert leaked not in resp.text
    # the full detail is still available locally
    assert any("sk-not-a-real-secret" in why for why in gateway.load_errors().values())


def test_load_error_summary_names_entry_points_by_name_only(tmp_path, monkeypatch):
    (tmp_path / "overlay_pkg_bad.py").write_text("raise RuntimeError('secret detail')")
    _fake_distribution(tmp_path, "overlay_pkg_bad", {"bad": "overlay_pkg_bad"})
    monkeypatch.syspath_prepend(str(tmp_path))
    gateway.load_integrations(force=True)
    assert gateway.load_error_summary() == {"entry point bad": "RuntimeError"}


# ── a failed load leaves nothing behind ────────────────────────────────────
REGISTER_THEN_RAISE = PLUGIN + "\nraise {exc}\n"


def test_a_plugin_that_registers_then_raises_is_not_left_registered(tmp_path, monkeypatch):
    (tmp_path / "zz_half.py").write_text(
        REGISTER_THEN_RAISE.format(cls="Half", name="overlay-half", word="halfway",
                                   exc="RuntimeError('config missing')"))
    monkeypatch.setenv("VOXWIRE_PLUGIN_PATH", str(tmp_path))
    names = gateway.load_integrations(force=True)
    assert "overlay-half" not in names
    assert any("zz_half" in where for where in gateway.load_errors())
    assert gateway.dispatch("halfway there")["handled_by"] != "overlay-half"


def test_a_failed_plugin_cannot_displace_an_integration_it_shares_a_name_with(tmp_path, monkeypatch):
    (tmp_path / "zz_shadow.py").write_text(
        REGISTER_THEN_RAISE.format(cls="Shadow", name="echo", word="shadowword",
                                   exc="RuntimeError('boom')"))
    monkeypatch.setenv("VOXWIRE_PLUGIN_PATH", str(tmp_path))
    echo = next(i for i in gateway.base.registered() if i.name == "echo")
    gateway.load_integrations(force=True)
    assert next(i for i in gateway.base.registered() if i.name == "echo") is echo


def test_an_entry_point_module_that_registers_then_raises_is_rolled_back(tmp_path, monkeypatch):
    (tmp_path / "overlay_pkg_half.py").write_text(
        REGISTER_THEN_RAISE.format(cls="EpHalf", name="overlay-ep-half", word="mitad",
                                   exc="RuntimeError('config missing')"))
    _fake_distribution(tmp_path, "overlay_pkg_half", {"half": "overlay_pkg_half"})
    monkeypatch.syspath_prepend(str(tmp_path))
    assert "overlay-ep-half" not in gateway.load_integrations(force=True)
    assert any("overlay_pkg_half" in where for where in gateway.load_errors())


def test_a_plugin_that_calls_sys_exit_is_skipped_not_fatal(tmp_path, monkeypatch):
    (tmp_path / "aa_exits.py").write_text(
        REGISTER_THEN_RAISE.format(cls="Exits", name="overlay-exits", word="adios",
                                   exc="SystemExit('missing token')"))
    (tmp_path / "zz_after.py").write_text(PLUGIN.format(cls="After", name="overlay-after", word="luego"))
    monkeypatch.setenv("VOXWIRE_PLUGIN_PATH", str(tmp_path))
    names = gateway.load_integrations(force=True)
    assert "overlay-exits" not in names and "overlay-after" in names
    assert any("aa_exits" in where and "SystemExit" in why
               for where, why in gateway.load_errors().items())


def test_ctrl_c_during_plugin_load_still_interrupts(tmp_path, monkeypatch):
    import pytest
    (tmp_path / "zz_interrupt.py").write_text("raise KeyboardInterrupt")
    monkeypatch.setenv("VOXWIRE_PLUGIN_PATH", str(tmp_path))
    with pytest.raises(KeyboardInterrupt):
        gateway.load_integrations(force=True)
