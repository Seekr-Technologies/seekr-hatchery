"""Unit tests for mount_links.expand_link_mounts — resolving symlinked entries."""

import os
from pathlib import Path

import pytest

import seekr_hatchery.mount_links as mount_links
from seekr_hatchery.mount import BindMount, TmpfsMount, VolumeMount

DST = "/home/hatchery/.claude/skills"


@pytest.fixture()
def external(tmp_path: Path) -> Path:
    """A directory outside the fake home, standing in for a dotfiles repo.

    Sibling of the autouse ``home`` fixture's directory, so symlinks into
    it are genuinely external the way a real ``~/.dotfiles`` link is.
    """
    d = tmp_path / "external"
    d.mkdir()
    return d


def _skill(parent: Path, name: str) -> Path:
    """Create a realistic skill directory (a dir holding SKILL.md)."""
    d = parent / name
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text("# skill\n")
    return d


def _bind(src: Path, dst: str = DST, **kw) -> BindMount:
    return BindMount(src=src, dst=dst, follow_links=True, **kw)


def _plain(src: Path, dst: str = DST, **kw) -> BindMount:
    """The flag-cleared form of a follow_links bind, as expand() emits it."""
    return BindMount(src=src, dst=dst, follow_links=False, **kw)


# ── Pass-through ──────────────────────────────────────────────────────────────


class TestPassThrough:
    def test_non_bind_mounts_untouched(self):
        mounts = [VolumeMount(name="claude-dir", dst="/home/hatchery/.claude"), TmpfsMount(dst="/tmp/x")]
        assert mount_links.expand_link_mounts(mounts) == mounts

    def test_bind_without_flag_untouched(self, home, external):
        skills = home / ".claude" / "skills"
        skills.mkdir(parents=True)
        (skills / "linked").symlink_to(_skill(external, "linked"))
        mounts = [BindMount(src=skills, dst=DST, mode="RW")]
        assert mount_links.expand_link_mounts(mounts) == mounts

    def test_missing_src_passes_through_unexpanded(self, home):
        # Backends guard with exists(), but expand() must not invent mounts.
        missing = home / ".claude" / "skills"
        assert mount_links.expand_link_mounts([_bind(missing)]) == [_plain(missing)]

    def test_flag_on_a_file_src_is_inert(self, home, external):
        """`-v` resolves a mount's source, so a symlinked file already works.

        The flag being *inert* here rather than merely unnecessary is a
        documented guarantee — backends declare it uniformly across a group of
        config paths without checking which of them are files. Note ``_bind``
        sets the flag: this asserts declaring it changes nothing.
        """
        (external / "CLAUDE.md").write_text("# global\n")
        link = home / ".claude" / "CLAUDE.md"
        link.parent.mkdir(parents=True)
        link.symlink_to(external / "CLAUDE.md")
        dst = "/home/hatchery/.claude/CLAUDE.md"

        assert mount_links.expand_link_mounts([_bind(link, dst)]) == [_plain(link, dst)]
        # ...and identical to not declaring it at all, bar the cleared flag.
        assert mount_links.expand_link_mounts([_plain(link, dst)]) == [_plain(link, dst)]

    def test_flag_on_a_real_file_src_is_inert(self, home):
        # Not just symlinked files — an ordinary one too.
        f = home / ".claude" / "CLAUDE.md"
        f.parent.mkdir(parents=True)
        f.write_text("# global\n")
        dst = "/home/hatchery/.claude/CLAUDE.md"
        assert mount_links.expand_link_mounts([_bind(f, dst)]) == [_plain(f, dst)]

    def test_dir_without_symlinks_gets_no_companion(self, home):
        skills = home / ".claude" / "skills"
        _skill(skills, "real")
        assert mount_links.expand_link_mounts([_bind(skills)]) == [_plain(skills)]


# ── The rule: mount the target where the link points, container-side ──────────


class TestAbsoluteLinks:
    def test_target_mounted_at_its_own_host_path(self, home, external):
        skills = home / ".claude" / "skills"
        skills.mkdir(parents=True)
        target = _skill(external, "panel-review")
        (skills / "panel-review").symlink_to(target)

        assert mount_links.expand_link_mounts([_bind(skills)]) == [
            _plain(skills),
            BindMount(src=target, dst=str(target), mode="RW"),
        ]

    def test_parent_stays_bound_whole_alongside_real_entries(self, home, external):
        # The user's actual layout: one real skill beside one symlinked one.
        skills = home / ".claude" / "skills"
        _skill(skills, "agent-comm")
        target = _skill(external, "panel-review")
        (skills / "panel-review").symlink_to(target)

        result = mount_links.expand_link_mounts([_bind(skills)])
        # skills/ itself is still a single mount — no partitioning.
        assert [m.dst for m in result] == [DST, str(target)]

    def test_internal_target_still_needs_its_host_path(self, home):
        """An absolute link pointing *inside* the scanned directory.

        The mirrored counterpart of this needs nothing —
        ``TestMirroredTree.test_absolute_internal_symlink_skipped`` — because
        there the mount already puts content at the path the link stores.
        Here it does not: ``dst`` is under ``/home/hatchery`` while the link
        says ``<host home>/.claude/skills/real``. So the target is mounted a
        second time, at its host path, even though the same bytes are already
        visible at ``{DST}/real``. Redundant and unavoidable: the link's
        stored text is fixed, and nothing rewrites it.
        """
        skills = home / ".claude" / "skills"
        target = _skill(skills, "real")
        (skills / "alias").symlink_to(target)  # absolute, and internal

        assert mount_links.expand_link_mounts([_bind(skills)]) == [
            _plain(skills),
            BindMount(src=target, dst=str(target), mode="RW"),
        ]

    def test_chain_resolves_at_the_first_hop(self, home, external):
        # Final target mounted at hop 1's destination, so the whole chain
        # resolves in one lookup and hop 2 is never walked.
        skills = home / ".claude" / "skills"
        skills.mkdir(parents=True)
        final = _skill(external, "final")
        hop = external / "hop"
        hop.symlink_to(final)
        (skills / "skill").symlink_to(hop)

        assert mount_links.expand_link_mounts([_bind(skills)]) == [
            _plain(skills),
            BindMount(src=final, dst=str(hop), mode="RW"),
        ]


class TestRelativeLinks:
    """The shape the worktree scan cannot express. GNU stow's default."""

    def test_target_mounted_at_container_side_resolution(self, home, external):
        skills = home / ".claude" / "skills"
        skills.mkdir(parents=True)
        target = _skill(external, "panel-review")
        (skills / "panel-review").symlink_to(
            os.path.relpath(target, skills),
            target_is_directory=True,
        )

        # DST is /home/hatchery/.claude/skills, and the link climbs out of
        # home to a sibling of it, so the container-side resolution is
        # rooted at /home/hatchery's parent — not the host path.
        expected = os.path.normpath(f"{DST}/{os.path.relpath(target, skills)}")
        assert expected != str(target)
        assert mount_links.expand_link_mounts([_bind(skills)]) == [
            _plain(skills),
            BindMount(src=target, dst=expected, mode="RW"),
        ]

    def test_link_into_the_same_mount_needs_nothing(self, home):
        skills = home / ".claude" / "skills"
        _skill(skills, "real")
        (skills / "alias").symlink_to("real", target_is_directory=True)
        assert mount_links.expand_link_mounts([_bind(skills)]) == [_plain(skills)]

    def test_link_into_another_scanned_mount_needs_nothing(self, home):
        claude = home / ".claude"
        _skill(claude / "agents", "reviewer")
        skills = claude / "skills"
        skills.mkdir(parents=True)
        (skills / "reviewer").symlink_to("../agents/reviewer", target_is_directory=True)

        agents_dst = "/home/hatchery/.claude/agents"
        mounts = [_bind(skills), _bind(claude / "agents", agents_dst)]
        assert mount_links.expand_link_mounts(mounts) == [_plain(skills), _plain(claude / "agents", agents_dst)]


class TestDepth:
    def test_link_nested_inside_a_real_entry_is_found(self, home, external):
        """Links are looked for at any depth, not just among direct entries.

        A skill directory that is real but holds a symlinked ``references/``
        used to dangle when only direct children were inspected.
        """
        skills = home / ".claude" / "skills"
        real = _skill(skills, "my-skill")
        target = _skill(external, "refs")
        (real / "references").symlink_to(target, target_is_directory=True)

        assert mount_links.expand_link_mounts([_bind(skills)]) == [
            _plain(skills),
            BindMount(src=target, dst=str(target), mode="RW"),
        ]

    def test_heavyweight_dirs_are_pruned(self, home, external):
        skills = home / ".claude" / "skills"
        nm = skills / "my-skill" / "node_modules"
        nm.mkdir(parents=True)
        (nm / "link").symlink_to(_skill(external, "dep"), target_is_directory=True)

        assert mount_links.expand_link_mounts([_bind(skills)]) == [_plain(skills)]


class TestMode:
    def test_mode_propagates_to_the_companion(self, home, external):
        skills = home / ".claude" / "skills"
        skills.mkdir(parents=True)
        target = _skill(external, "s")
        (skills / "s").symlink_to(target)

        result = mount_links.expand_link_mounts([_bind(skills, mode="RO")])
        assert [m.mode for m in result] == ["RO", "RO"]


# ── Guards ────────────────────────────────────────────────────────────────────


class TestSkipped:
    def test_broken_symlink_dropped(self, home, external):
        skills = home / ".claude" / "skills"
        skills.mkdir(parents=True)
        (skills / "gone").symlink_to(external / "nope")
        assert mount_links.expand_link_mounts([_bind(skills)]) == [_plain(skills)]

    def test_symlink_loop_dropped(self, home):
        skills = home / ".claude" / "skills"
        skills.mkdir(parents=True)
        (skills / "a").symlink_to(skills / "b")
        (skills / "b").symlink_to(skills / "a")
        assert mount_links.expand_link_mounts([_bind(skills)]) == [_plain(skills)]

    def test_colon_in_target_dropped(self, home, external):
        skills = home / ".claude" / "skills"
        skills.mkdir(parents=True)
        (skills / "odd").symlink_to(_skill(external, "a:b"))
        assert mount_links.expand_link_mounts([_bind(skills)]) == [_plain(skills)]

    def test_non_regular_target_dropped(self, home, external):
        skills = home / ".claude" / "skills"
        skills.mkdir(parents=True)
        fifo = external / "pipe"
        os.mkfifo(fifo)
        (skills / "pipe").symlink_to(fifo)
        assert mount_links.expand_link_mounts([_bind(skills)]) == [_plain(skills)]

    def test_unreadable_dir_skipped(self, home, monkeypatch):
        skills = home / ".claude" / "skills"
        skills.mkdir(parents=True)

        def boom(*a, **kw):
            raise PermissionError("nope")

        # os.walk routes scandir failures to its onerror hook; patch there
        # rather than at Path.iterdir, which the walk does not use.
        monkeypatch.setattr(os, "scandir", boom)
        assert mount_links.expand_link_mounts([_bind(skills)]) == [_plain(skills)]


class TestSystemPathsAreNotThisModulesJob:
    """Expansion emits these; ``docker._validate_mounts`` drops them.

    Kept here as the expansion half of the seam — a bad mount is equally
    reachable by asking for one directly, so the decision belongs to one
    validation pass over the finished list, not to each producer. The drop
    itself is asserted in ``test_docker.py::TestValidateMounts``.
    """

    def test_system_path_target_is_emitted_not_filtered(self, home):
        skills = home / ".claude" / "skills"
        skills.mkdir(parents=True)
        (skills / "usr").symlink_to("/usr/share")

        assert mount_links.expand_link_mounts([_bind(skills)]) == [
            _plain(skills),
            BindMount(src=Path("/usr/share"), dst="/usr/share", mode="RW"),
        ]

    def test_relative_link_escaping_into_a_system_path_is_emitted(self, home, tmp_path):
        # The host target resolves fine, but the same relative offset applied
        # to the *container* directory lands in /usr. The two sides differ
        # because DST is exactly four components deep while the host directory
        # is deeper — the hazard in miniature, and why the destination has to
        # be checked at all.
        skills = home / ".claude" / "skills"
        skills.mkdir(parents=True)
        raw = "../../../../usr/lib/foo"
        target = _skill(tmp_path.parent, "usr/lib/foo")
        (skills / "foo").symlink_to(raw, target_is_directory=True)

        assert os.path.normpath(f"{DST}/{raw}") == "/usr/lib/foo"
        assert mount_links.expand_link_mounts([_bind(skills)]) == [
            _plain(skills),
            BindMount(src=target, dst="/usr/lib/foo", mode="RW"),
        ]


class TestDedup:
    def test_two_links_onto_one_path_emit_one_mount(self, home, external):
        skills = home / ".claude" / "skills"
        skills.mkdir(parents=True)
        target = _skill(external, "shared")
        (skills / "one").symlink_to(target)
        (skills / "two").symlink_to(target)

        assert mount_links.expand_link_mounts([_bind(skills)]) == [
            _plain(skills),
            BindMount(src=target, dst=str(target), mode="RW"),
        ]


class TestIdempotence:
    def test_expanding_twice_changes_nothing(self, home, external):
        skills = home / ".claude" / "skills"
        _skill(skills, "real")
        (skills / "linked").symlink_to(_skill(external, "linked"))

        once = mount_links.expand_link_mounts([_bind(skills)])
        assert mount_links.expand_link_mounts(once) == once


# ── Container-side coverage ───────────────────────────────────────────────────


class TestAlreadyProvided:
    """A link needs nothing when its *container-side* destination is mounted.

    Coverage has to be judged on the container side, not the host side: a
    mount only makes a stored path resolve when its ``dst`` is that path.
    """

    def test_link_into_a_mirrored_mount_needs_nothing(self, home, external):
        skills = home / ".claude" / "skills"
        skills.mkdir(parents=True)
        target = _skill(external, "s")
        (skills / "s").symlink_to(target)

        # A mount of external at its own host path already provides the path
        # the link stores, so no companion is needed.
        mirrored = BindMount(src=external, dst=str(external), mode="RO")
        assert mount_links.expand_link_mounts([_bind(skills), mirrored]) == [_plain(skills), mirrored]

    def test_link_into_a_relocated_mount_still_needs_a_companion(self, home, external):
        skills = home / ".claude" / "skills"
        skills.mkdir(parents=True)
        target = _skill(external, "s")
        (skills / "s").symlink_to(target)

        # external IS mounted, but at /elsewhere — so the host path the link
        # stores does not exist in the container and the link still dangles.
        relocated = BindMount(src=external, dst="/elsewhere", mode="RO")
        assert mount_links.expand_link_mounts([_bind(skills), relocated]) == [
            _plain(skills),
            relocated,
            BindMount(src=target, dst=str(target), mode="RW"),
        ]

    def test_ancestor_of_an_existing_mount_dropped(self, home, external):
        skills = home / ".claude" / "skills"
        skills.mkdir(parents=True)
        (skills / "up").symlink_to(external)

        # Mounting external would sit above an existing mount and bury
        # whatever else the image keeps at that path.
        inner = BindMount(src=_skill(external, "inner"), dst=f"{external}/inner", mode="RO")
        assert mount_links.expand_link_mounts([_bind(skills), inner]) == [_plain(skills), inner]


# ── The mirrored case (the worktree) ──────────────────────────────────────────


class TestMirroredTree:
    """A tree mounted at its own host path — the worktree shape.

    These are the old ``docker._construct_symlink_mounts`` tests, kept
    assertion-for-assertion, because the argument for deleting that scanner is
    that it was the degenerate case of this rule: when ``dst == src``,
    ``normpath(container_dir + raw) == normpath(host_dir + raw)``, so both link
    shapes land on the ``target:target`` mount it used to emit by hand.
    """

    def _companions(self, scan: Path, existing: tuple[BindMount, ...] = ()) -> list[BindMount]:
        """Expand a mirrored mount over *scan*; return just the additions."""
        root = BindMount(src=scan, dst=str(scan), mode="RW", follow_links=True)
        out = mount_links.expand_link_mounts([root, *existing])
        head, companions = out[: 1 + len(existing)], out[1 + len(existing) :]
        assert head == [root.model_copy(update={"follow_links": False}), *existing]
        return companions  # type: ignore[return-value]

    def test_external_file_symlink_emits_mount(self, tmp_path, external):
        scan = tmp_path / "worktree"
        scan.mkdir()
        target = external / "file.txt"
        target.write_text("hello")
        (scan / "link").symlink_to(target)

        assert self._companions(scan) == [BindMount(src=target, dst=str(target), mode="RW")]

    def test_external_dir_symlink_emits_mount(self, tmp_path, external):
        scan = tmp_path / "worktree"
        scan.mkdir()
        target = _skill(external, "dir")
        (scan / "linkdir").symlink_to(target)

        assert self._companions(scan) == [BindMount(src=target, dst=str(target), mode="RW")]

    def test_relative_internal_symlink_skipped(self, tmp_path):
        scan = tmp_path / "worktree"
        scan.mkdir()
        (scan / "inner.txt").write_text("x")
        (scan / "link").symlink_to("inner.txt")

        assert self._companions(scan) == []

    def test_absolute_internal_symlink_skipped(self, tmp_path):
        # Mirroring is what makes this one free: the stored absolute path is
        # inside the tree's own mount.
        scan = tmp_path / "worktree"
        scan.mkdir()
        (scan / "inner.txt").write_text("x")
        (scan / "link").symlink_to(scan / "inner.txt")

        assert self._companions(scan) == []

    def test_relative_external_link_emits_mount(self, tmp_path, external):
        scan = tmp_path / "worktree"
        scan.mkdir()
        (scan / "link").symlink_to(os.path.relpath(external, scan), target_is_directory=True)

        # The mirrored case: the relative climb lands on the host path, so this
        # is the same mount an absolute link would have produced.
        assert self._companions(scan) == [BindMount(src=external, dst=str(external), mode="RW")]

    def test_dedupes_same_target(self, tmp_path, external):
        scan = tmp_path / "worktree"
        scan.mkdir()
        target = external / "file.txt"
        target.write_text("hello")
        (scan / "a").symlink_to(target)
        (scan / "b").symlink_to(target)

        assert self._companions(scan) == [BindMount(src=target, dst=str(target), mode="RW")]

    def test_broken_symlink_skipped(self, tmp_path):
        scan = tmp_path / "worktree"
        scan.mkdir()
        (scan / "broken").symlink_to(tmp_path / "does-not-exist")

        assert self._companions(scan) == []

    def test_system_path_target_is_emitted_not_filtered(self, tmp_path):
        # The old scanner dropped this itself; now it survives expansion and
        # docker._validate_mounts drops it. Same outcome at launch, one place.
        scan = tmp_path / "worktree"
        scan.mkdir()
        (scan / "syslink").symlink_to("/usr/bin/env")

        assert self._companions(scan) == [BindMount(src=Path("/usr/bin/env"), dst="/usr/bin/env", mode="RW")]

    def test_nested_external_target(self, tmp_path, external):
        # Found at any depth, not just among direct entries.
        scan = tmp_path / "worktree"
        nested = scan / "a" / "b"
        nested.mkdir(parents=True)
        target = _skill(external, "data")
        (nested / "link").symlink_to(target)

        assert self._companions(scan) == [BindMount(src=target, dst=str(target), mode="RW")]

    def test_nested_relative_internal_symlink_skipped(self, tmp_path):
        scan = tmp_path / "worktree"
        (scan / "a").mkdir(parents=True)
        (scan / "b").mkdir()
        (scan / "b" / "file.txt").write_text("x")
        (scan / "a" / "link").symlink_to("../b/file.txt")

        # Resolved against the *nested* container directory, not the root.
        assert self._companions(scan) == []

    def test_heavyweight_dir_pruned(self, tmp_path, external):
        scan = tmp_path / "worktree"
        scan.mkdir()
        target = external / "file.txt"
        target.write_text("x")
        node_modules = scan / "node_modules"
        node_modules.mkdir()
        (node_modules / "link").symlink_to(target)

        assert self._companions(scan) == []

    def test_symlinked_scan_root_is_walked_through(self, tmp_path, external):
        """The scanned directory may itself be a symlink.

        Needs no flag to *mount* correctly — ``-v`` resolves the source — but
        the walk still has to read through it to find the links inside.
        """
        real = _skill(external, "tree")
        target = _skill(external, "linked")
        (real / "entry").symlink_to(target, target_is_directory=True)
        scan = tmp_path / "worktree"
        scan.symlink_to(real, target_is_directory=True)

        assert self._companions(scan) == [BindMount(src=target, dst=str(target), mode="RW")]
