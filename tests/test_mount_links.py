"""Unit tests for mount_links.expand_link_mounts — resolving symlinked entries."""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

import seekr_hatchery.mount as mount
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


class TestRecursiveLinks:
    @pytest.mark.parametrize("child_first", [True, False])
    def test_discovered_parent_is_not_blocked_by_generated_child(
        self,
        tmp_path: Path,
        child_first: bool,
    ) -> None:
        package = tmp_path / "external/package"
        child = package / "subdir"
        child.mkdir(parents=True)
        dependency = tmp_path / "external/dependency"
        dependency.write_text("resource")
        (package / "dependency").symlink_to(dependency)
        child_root, parent_root = tmp_path / "child-root", tmp_path / "parent-root"
        child_root.mkdir()
        parent_root.mkdir()
        (child_root / "link").symlink_to(child)
        (parent_root / "link").symlink_to(package)
        declared = [
            BindMount(src=child_root, dst="/inputs/child", follow_links="RECURSIVE"),
            BindMount(src=parent_root, dst="/inputs/parent", follow_links="RECURSIVE"),
        ]
        if not child_first:
            declared.reverse()
        expected = [
            *(m.model_copy(update={"follow_links": False}) for m in declared),
            BindMount(src=package, dst=str(package)),
        ]
        if child_first:
            expected.append(BindMount(src=child, dst=str(child)))
        expected.append(BindMount(src=dependency, dst=str(dependency)))

        result = mount_links.expand_link_mounts(declared)
        assert result == expected
        assert mount_links.expand_link_mounts(result) == expected

    @pytest.mark.parametrize("follow_links", [True, "SHALLOW", "RECURSIVE"])
    @pytest.mark.parametrize("absolute", [False, True])
    def test_nested_targets_use_container_paths(self, tmp_path: Path, follow_links: bool | str, absolute: bool) -> None:
        host = tmp_path / "host"
        root = host / "root"
        package = host / "packages/pkg"
        dependency = host / "packages/dep"
        resources = host / "resources"
        for directory in (root, package, dependency, resources):
            directory.mkdir(parents=True)
        document = resources / "readme"
        document.write_text("dependency")
        (root / "pkg").symlink_to(package if absolute else "../packages/pkg")
        (package / "dependency").symlink_to("../dep")
        (dependency / "readme").symlink_to("../../resources/readme")
        original = BindMount(src=root, dst="/sandbox/root", mode="RO", follow_links=follow_links)
        base = str(host) if absolute else "/sandbox"
        expected = [
            BindMount(src=root, dst="/sandbox/root", mode="RO"),
            BindMount(src=package, dst=f"{base}/packages/pkg", mode="RO"),
        ]
        if follow_links == "RECURSIVE":
            expected.extend(
                [
                    BindMount(src=dependency, dst=f"{base}/packages/dep", mode="RO"),
                    BindMount(src=document, dst=f"{base}/resources/readme", mode="RO"),
                ]
            )

        result = mount_links.expand_link_mounts([original])
        assert result == expected
        assert mount_links.expand_link_mounts(result) == expected

    def test_directory_cycle_stops_without_global_alias_suppression(self, tmp_path: Path) -> None:
        root = tmp_path / "root"
        package = tmp_path / "package"
        root.mkdir()
        package.mkdir()
        alias = tmp_path / "alias"
        alias.symlink_to(package)
        (root / "package").symlink_to(package)
        (package / "cycle").symlink_to(alias)
        # The cycle resolves to the same host directory at a new container
        # destination. Mount that first hop but do not scan it forever.
        assert mount_links.expand_link_mounts(
            [
                BindMount(src=root, dst=DST, follow_links="RECURSIVE"),
            ]
        ) == [
            BindMount(src=root, dst=DST),
            BindMount(src=package, dst=str(package)),
            BindMount(src=package, dst=str(alias)),
        ]

    def test_aliases_are_scanned_at_each_container_location(self, tmp_path: Path) -> None:
        root = tmp_path / "host/root"
        root.mkdir(parents=True)
        package = tmp_path / "host/pkg"
        package.mkdir()
        document = tmp_path / "host/readme"
        document.write_text("resource")
        (package / "readme").symlink_to("../readme")
        # Two different first hops resolve to the same host target.
        (tmp_path / "host/other").mkdir()
        (tmp_path / "host/other/alias").symlink_to(package)
        (root / "one").symlink_to("../pkg")
        (root / "two").symlink_to("../other/alias")
        assert mount_links.expand_link_mounts(
            [
                BindMount(src=root, dst="/sandbox/root", follow_links="RECURSIVE"),
            ]
        ) == [
            BindMount(src=root, dst="/sandbox/root"),
            BindMount(src=package, dst="/sandbox/pkg"),
            BindMount(src=package, dst="/sandbox/other/alias"),
            BindMount(src=document, dst="/sandbox/readme"),
            BindMount(src=document, dst="/sandbox/other/readme"),
        ]


# ── The mirrored case (the worktree) ──────────────────────────────────────────


@pytest.mark.parametrize("shape", ["file", "directory", "directory_link"])
@pytest.mark.parametrize("follow_links", [True, "SHALLOW", "RECURSIVE"])
def test_shadowed_paths_do_not_export_symlink_targets(tmp_path: Path, shape: str, follow_links: bool | str) -> None:
    root = tmp_path / "root"
    root.mkdir()
    secret = tmp_path / "secret"
    secret.write_text("host-secret")
    hidden = root / "hidden"
    if shape == "file":
        hidden.symlink_to(secret)
        overlay = BindMount(src=tmp_path / "safe", dst=f"{DST}/hidden")
    else:
        directory = root / "hidden" if shape == "directory" else tmp_path / "external"
        directory.mkdir()
        (directory / "credential").symlink_to(secret)
        if shape == "directory_link":
            hidden.symlink_to(directory)
        overlay = VolumeMount(name="local", dst=f"{DST}/hidden")
    visible = tmp_path / "APPEND_SYSTEM.md"
    visible.write_text("user instructions")
    (root / "APPEND_SYSTEM.md").symlink_to(visible)

    assert mount_links.expand_link_mounts([BindMount(src=root, dst=DST, follow_links=follow_links), overlay]) == [
        _plain(root),
        overlay,
        BindMount(src=visible, dst=str(visible)),
    ]


@pytest.mark.parametrize("follow_links", [True, "SHALLOW", "RECURSIVE"])
@pytest.mark.parametrize("alias_kind", ["directory", "ancestor", "file", "file_target", "volume_child"])
def test_aliases_cannot_expose_shadowed_host_sources(
    tmp_path: Path,
    follow_links: bool | str,
    alias_kind: str,
) -> None:
    root = tmp_path / "agent"
    root.mkdir()
    credential = root / "auth.json"
    external = tmp_path / "private-auth"
    external.write_text("host-secret")
    credential.symlink_to(external)
    sessions = root / "sessions"
    sessions.mkdir()
    history = sessions / "history"
    history.write_text("host-history")
    targets = {
        "directory": root,
        "ancestor": tmp_path,
        "file": credential,
        "file_target": external,
        "volume_child": history,
    }
    (root / "alias").symlink_to(targets[alias_kind])
    visible = tmp_path / "instructions.md"
    visible.write_text("shared")
    (root / "instructions").symlink_to(visible)
    overlays = [
        BindMount(src=tmp_path / "task-auth", dst=f"{DST}/auth.json"),
        VolumeMount(name="sessions", dst=f"{DST}/sessions"),
    ]
    expected = [BindMount(src=root, dst=DST), *overlays, BindMount(src=visible, dst=str(visible))]

    assert (
        mount_links.expand_link_mounts(
            [
                BindMount(src=root, dst=DST, follow_links=follow_links),
                *overlays,
            ]
        )
        == expected
    )


def test_aliases_of_unrestricted_nested_binds_still_work(tmp_path: Path) -> None:
    root = tmp_path / "root"
    nested = root / "nested"
    nested.mkdir(parents=True)
    (root / "alias").symlink_to(nested)
    overlay = BindMount(src=nested, dst=f"{DST}/nested")
    assert mount_links.expand_link_mounts(
        [
            BindMount(src=root, dst=DST, follow_links="RECURSIVE"),
            overlay,
        ]
    ) == [
        BindMount(src=root, dst=DST),
        overlay,
        BindMount(src=nested, dst=str(nested)),
    ]


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

        # Some distributions symlink env to another coreutils installation.
        # Sources resolve fully; destinations retain the first symlink hop.
        assert self._companions(scan) == [BindMount(src=Path("/usr/bin/env").resolve(), dst="/usr/bin/env", mode="RW")]

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


@pytest.mark.parametrize("live", [False, pytest.param(True, marks=pytest.mark.integration)])
@pytest.mark.parametrize("parent_first", [True, False])
@pytest.mark.parametrize("parent_mode,child_mode", [("RW", "RO"), ("RO", "RW")])
@pytest.mark.parametrize("child_follow", [True, "RECURSIVE"])
def test_generated_child_preserves_mode_and_recursive_dependencies(
    tmp_path: Path, parent_first: bool, parent_mode: str, child_mode: str, child_follow: bool | str, live: bool
) -> None:
    package = tmp_path / "package"
    child = package / "child"
    child.mkdir(parents=True)
    (child / "data").write_text("original")
    dependency = tmp_path / "dependency"
    dependency.write_text("resource")
    (child / "dependency").symlink_to(dependency)
    parent_root, child_root = tmp_path / "parent-root", tmp_path / "child-root"
    parent_root.mkdir()
    child_root.mkdir()
    (parent_root / "package").symlink_to(package)
    (child_root / "child").symlink_to(child)
    parent_mount = BindMount(src=parent_root, dst="/inputs/parent", mode=parent_mode, follow_links="RECURSIVE")
    child_mount = BindMount(src=child_root, dst="/inputs/child", mode=child_mode, follow_links=child_follow)
    declared = [parent_mount, child_mount] if parent_first else [child_mount, parent_mount]
    expected = [
        *(m.model_copy(update={"follow_links": False}) for m in declared),
        BindMount(src=package, dst=str(package), mode=parent_mode),
        BindMount(src=child, dst=str(child), mode=child_mode),
        BindMount(src=dependency, dst=str(dependency), mode=child_mode),
    ]
    result = mount_links.expand_link_mounts(declared)
    assert result == expected
    assert mount_links.expand_link_mounts(result) == expected
    if live:
        output = _run_link_container(
            result,
            "cat /inputs/child/child/dependency; "
            "if echo changed > /inputs/child/child/data 2>/dev/null; then echo WRITABLE; else echo BLOCKED; fi; "
            "if echo changed > /inputs/child/child/dependency 2>/dev/null; then echo WRITABLE; else echo BLOCKED; fi",
        )
        assert output == {"RO": "resourceBLOCKED\nBLOCKED\n", "RW": "resourceWRITABLE\nWRITABLE\n"}[child_mode]
        assert (child / "data").read_text() == {"RO": "original", "RW": "changed\n"}[child_mode]


@pytest.mark.parametrize("live", [False, pytest.param(True, marks=pytest.mark.integration)])
@pytest.mark.parametrize("recursive_first", [True, False])
@pytest.mark.parametrize("shallow", [True, "SHALLOW"])
def test_recursive_scan_is_not_satisfied_by_shallow_companion(
    tmp_path: Path, recursive_first: bool, shallow: bool | str, live: bool
) -> None:
    package = tmp_path / "package"
    package.mkdir()
    dependency = tmp_path / "dependency"
    dependency.write_text("resource")
    (package / "dependency").symlink_to(dependency)
    shallow_root, recursive_root = tmp_path / "shallow", tmp_path / "recursive"
    shallow_root.mkdir()
    recursive_root.mkdir()
    (shallow_root / "package").symlink_to(package)
    (recursive_root / "package").symlink_to(package)
    shallow_mount = BindMount(src=shallow_root, dst="/shallow", follow_links=shallow)
    recursive_mount = BindMount(src=recursive_root, dst="/recursive", follow_links="RECURSIVE")
    declared = [recursive_mount, shallow_mount] if recursive_first else [shallow_mount, recursive_mount]
    expected = [
        *(m.model_copy(update={"follow_links": False}) for m in declared),
        BindMount(src=package, dst=str(package)),
        BindMount(src=dependency, dst=str(dependency)),
    ]
    result = mount_links.expand_link_mounts(declared)
    assert result == expected
    assert mount_links.expand_link_mounts(result) == expected
    if live:
        assert _run_link_container(result, "cat /recursive/package/dependency") == "resource"


def _run_link_container(mounts: list[mount.Mount], script: str) -> str:
    if shutil.which("podman") is None or subprocess.run(["podman", "info"], capture_output=True).returncode:
        pytest.skip("podman unavailable")
    result = subprocess.run(
        [
            "podman",
            "run",
            "--rm",
            *(arg for item in mounts for arg in mount.mount_to_docker_args(item)),
            "docker.io/library/alpine:3.21",
            "sh",
            "-c",
            script,
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout


@pytest.mark.parametrize("parent_first", [True, False])
def test_generated_coverage_requires_matching_source_mapping(tmp_path: Path, parent_first: bool) -> None:
    first_root = tmp_path / "first/root"
    second_root = tmp_path / "second/root"
    package = tmp_path / "first/package"
    child = tmp_path / "second/package/child"
    for directory in (first_root, second_root, package / "child", child):
        directory.mkdir(parents=True)
    (first_root / "link").symlink_to("../package")
    (second_root / "link").symlink_to("../package/child")
    parent = BindMount(src=first_root, dst="/sandbox/root-one", follow_links="RECURSIVE")
    nested = BindMount(src=second_root, dst="/sandbox/root-two", follow_links="RECURSIVE")
    declared = [parent, nested] if parent_first else [nested, parent]
    assert mount_links.expand_link_mounts(declared) == [
        *(m.model_copy(update={"follow_links": False}) for m in declared),
        BindMount(src=package, dst="/sandbox/package"),
        BindMount(src=child, dst="/sandbox/package/child"),
    ]


def test_mixed_scan_strength_preserves_independent_relative_aliases(tmp_path: Path) -> None:
    root = tmp_path / "root"
    package = tmp_path / "package"
    root.mkdir()
    package.mkdir()
    dependency = tmp_path / "dependency"
    dependency.write_text("resource")
    (root / "package").symlink_to("../package")
    (package / "dependency").symlink_to("../dependency")
    assert mount_links.expand_link_mounts(
        [
            BindMount(src=root, dst="/one/root", follow_links=True),
            BindMount(src=root, dst="/two/root", follow_links="RECURSIVE"),
            BindMount(src=root, dst="/three/root", follow_links="RECURSIVE"),
        ]
    ) == [
        BindMount(src=root, dst="/one/root"),
        BindMount(src=root, dst="/two/root"),
        BindMount(src=root, dst="/three/root"),
        BindMount(src=package, dst="/one/package"),
        BindMount(src=package, dst="/two/package"),
        BindMount(src=package, dst="/three/package"),
        BindMount(src=dependency, dst="/two/dependency"),
        BindMount(src=dependency, dst="/three/dependency"),
    ]


@pytest.mark.parametrize("reverse", [False, True])
def test_exact_generated_destination_source_conflicts_remain_first_wins(tmp_path: Path, reverse: bool) -> None:
    first_root, second_root = tmp_path / "first/root", tmp_path / "second/root"
    first_package, second_package = tmp_path / "first/package", tmp_path / "second/package"
    for directory in (first_root, second_root, first_package, second_package):
        directory.mkdir(parents=True)
    (first_root / "link").symlink_to("../package")
    (second_root / "link").symlink_to("../package")
    first = BindMount(src=first_root, dst="/sandbox/first", mode="RO", follow_links="RECURSIVE")
    second = BindMount(src=second_root, dst="/sandbox/second", mode="RW", follow_links="RECURSIVE")
    declared = [second, first] if reverse else [first, second]
    winner = (
        BindMount(src=second_package, dst="/sandbox/package", mode="RW")
        if reverse
        else BindMount(src=first_package, dst="/sandbox/package", mode="RO")
    )
    assert mount_links.expand_link_mounts(declared) == [
        *(m.model_copy(update={"follow_links": False}) for m in declared),
        winner,
    ]
