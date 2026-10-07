from aa_arena.legacy import ai9


def test_compiler_change_invalidates_backend_and_player_source_digest(tmp_path, monkeypatch):
    (tmp_path / "ai.cpp").write_text("// immutable source\n")
    monkeypatch.setattr(ai9, "_compiler_identity", lambda: b"GCC 11")
    first = ai9._digest((tmp_path,))
    monkeypatch.setattr(ai9, "_compiler_identity", lambda: b"GCC 14.2")
    assert ai9._digest((tmp_path,)) != first


def test_backend_and_candidate_select_same_compiler(monkeypatch, tmp_path):
    monkeypatch.setenv("AA_ARENA_CXX", "g++-14")
    calls = []
    def execute(command, **kwargs):
        calls.append(command)
        from subprocess import CompletedProcess
        return CompletedProcess(command, 0, "", "")
    monkeypatch.setattr(ai9, "run_isolated_build", execute)
    ai9._run_player_build(["g++", "ai.cpp", "-o", "player.so"],
                          cwd=tmp_path, readonly_paths=())
    assert calls[0][0] == ai9._runtime_environment()["CXX"] == "g++-14"
