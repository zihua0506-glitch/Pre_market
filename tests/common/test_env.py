"""tests for common.env"""

from pathlib import Path

import pytest

from common.env import load_env, write_env


class TestLoadEnv:
    def test_parses_simple_kv(self, tmp_path: Path):
        p = tmp_path / ".env"
        p.write_text("FOO=bar\nBAZ=qux\n")
        env = load_env(p, env_prefixes=())  # disable os.environ harvest
        assert env == {"FOO": "bar", "BAZ": "qux"}

    def test_skips_comments_and_blanks(self, tmp_path: Path):
        p = tmp_path / ".env"
        p.write_text("# header\nFOO=bar\n\n  # indented\nBAZ=qux\n")
        env = load_env(p, env_prefixes=())
        assert env == {"FOO": "bar", "BAZ": "qux"}

    def test_handles_value_with_equals(self, tmp_path: Path):
        p = tmp_path / ".env"
        p.write_text("URL=https://example.com/?foo=bar&baz=qux\n")
        env = load_env(p, env_prefixes=())
        assert env["URL"] == "https://example.com/?foo=bar&baz=qux"

    def test_strips_whitespace(self, tmp_path: Path):
        p = tmp_path / ".env"
        p.write_text("  FOO  =  bar baz  \n")
        env = load_env(p, env_prefixes=())
        assert env == {"FOO": "bar baz"}

    def test_missing_file_returns_empty(self, tmp_path: Path):
        env = load_env(tmp_path / "does-not-exist.env", env_prefixes=())
        assert env == {}

    def test_env_var_overrides(self, tmp_path: Path, monkeypatch):
        p = tmp_path / ".env"
        p.write_text("POLY_API_KEY=from-file\n")
        monkeypatch.setenv("POLY_API_KEY", "from-environ")
        env = load_env(p)  # default prefixes include POLY_
        assert env["POLY_API_KEY"] == "from-environ"

    def test_env_prefix_filters(self, tmp_path: Path, monkeypatch):
        p = tmp_path / ".env"
        p.write_text("FOO=1\n")
        monkeypatch.setenv("POLY_KEY", "secret")
        monkeypatch.setenv("UNRELATED_KEY", "junk")
        env = load_env(p, env_prefixes=("POLY_",))
        assert env["POLY_KEY"] == "secret"
        assert "UNRELATED_KEY" not in env


class TestWriteEnv:
    def test_overwrites_existing_key(self, tmp_path: Path):
        p = tmp_path / ".env"
        p.write_text("FOO=old\nBAR=keep\n")
        write_env(p, {"FOO": "new"})
        assert p.read_text() == "FOO=new\nBAR=keep\n"

    def test_appends_new_keys(self, tmp_path: Path):
        p = tmp_path / ".env"
        p.write_text("FOO=keep\n")
        write_env(p, {"NEWKEY": "value"})
        # NEWKEY should be appended
        assert "FOO=keep" in p.read_text()
        assert "NEWKEY=value" in p.read_text()

    def test_preserves_comments(self, tmp_path: Path):
        p = tmp_path / ".env"
        p.write_text("# header comment\n\nFOO=old\n# inline section\nBAR=keep\n")
        write_env(p, {"FOO": "new"})
        out = p.read_text()
        assert "# header comment" in out
        assert "# inline section" in out
        assert "FOO=new" in out
        assert "BAR=keep" in out

    def test_creates_file_if_missing(self, tmp_path: Path):
        p = tmp_path / "fresh.env"
        write_env(p, {"FOO": "bar"})
        assert p.read_text() == "FOO=bar\n"

    def test_round_trip(self, tmp_path: Path):
        """write then load should yield the same dict."""
        p = tmp_path / ".env"
        write_env(p, {"FOO": "bar", "BAZ": "qux"})
        env = load_env(p, env_prefixes=())
        assert env == {"FOO": "bar", "BAZ": "qux"}
