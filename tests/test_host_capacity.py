from aa_arena.core.host_capacity import default_match_capacity


def topology(root, cpus):
    for cpu, package, core in cpus:
        p = root / f"cpu{cpu}" / "topology"
        p.mkdir(parents=True)
        (p / "physical_package_id").write_text(str(package))
        (p / "core_id").write_text(str(core))


def test_smt_does_not_double_match_capacity(tmp_path):
    topology(tmp_path, [(i, 0, i % 8) for i in range(16)])
    assert default_match_capacity(topology_root=tmp_path, cpu_ids=range(16)) == 3


def test_affinity_and_socket_identity_are_respected(tmp_path):
    topology(tmp_path, [(i, i // 4, i % 4) for i in range(8)])
    assert default_match_capacity(topology_root=tmp_path, cpu_ids=range(8)) == 3
    assert default_match_capacity(topology_root=tmp_path, cpu_ids=range(4)) == 1


def test_missing_topology_is_conservative(tmp_path):
    assert default_match_capacity(topology_root=tmp_path, cpu_ids=range(16)) == 3


def test_small_host_always_admits_a_match(tmp_path):
    topology(tmp_path, [(0, 0, 0)])
    assert default_match_capacity(topology_root=tmp_path, cpu_ids=[0]) == 1
