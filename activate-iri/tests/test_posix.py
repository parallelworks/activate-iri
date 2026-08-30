from activate_iri import posix


def test_parse_ls_handles_symlinks_and_spaces():
    out = ("total 8\n"
           "drwxr-xr-x 2 jdoe users 4096 2026-08-29T10:00:00+0000 my dir\n"
           "-rw-r--r-- 1 jdoe users  12 2026-08-29T10:01:00+0000 file.txt\n"
           "lrwxrwxrwx 1 jdoe users   8 2026-08-29T10:02:00+0000 link -> file.txt\n")
    entries = posix.parse_ls(out)
    assert [e.name for e in entries] == ["my dir", "file.txt", "link"]
    assert entries[0].type == "directory" and entries[2].type == "symlink" and entries[2].link_target == "file.txt"
    assert entries[1].permissions == "rw-r--r--" and entries[1].size == "12"


def test_parse_stat():
    st = posix.parse_stat("81a4 123 2049 1 1000 1000 12 1 2 3\n")
    assert st.mode == 0o100644 and st.size == 12 and st.uid == 1000


def test_scripts_quote_paths_and_guard_root():
    assert posix.ls_script("/tmp/a b", show_hidden=True).startswith("ls -la --time-style")
    assert "'/tmp/a b'" in posix.ls_script("/tmp/a b")
    for bad in ["/", "..", "/home", "/home/jdoe", "/tmp/x/../../etc", "relative/path", "/scratch/p1"]:
        try:
            posix.rm_script(bad)
            raise AssertionError(bad)
        except ValueError:
            pass
    assert posix.rm_script("/home/jdoe/work/old") == "rm -rf -- /home/jdoe/work/old"
    assert posix.compress_script("/tmp/x", "/tmp/x.tar.gz", "urn:doe-iri:compression:gzip").startswith("tar  -czf")


def test_parse_marked_output_requires_markers():
    from activate_iri.executor import parse_marked_output
    ok = parse_marked_output("noise\n__IRI_BEGIN__\n19\n__IRI_END__\nrc=0\n", default_rc=0, slug="x")
    assert ok.returncode == 0 and ok.stdout == "19\n"
    bad = parse_marked_output("", default_rc=0, slug="iri-exec-1")
    assert bad.returncode == 1 and "iri-exec-1" in bad.stderr
