import collections
import contextlib
import os
import shutil
import tempfile

from plypatch import exc
from plypatch import git
from plypatch import utils
from plypatch import version


__version__ = version.__version__


class WorkingRepo(git.Repo):
    """Represents our local fork of the upstream repository.

    This is where we will create new patches (save) or apply previous patches
    to create a new patch-branch (restore).
    """
    def _add_patch_annotation(self, patch_name):
        """Add a patch annotation to the last commit."""
        commit_msg = self.log(count=1, pretty='%B')
        if 'Ply-Patch' not in commit_msg:
            commit_msg += '\n\nPly-Patch: %s' % patch_name
            self.commit(commit_msg, amend=True)

    def _walk_commit_msgs_backwards(self):
        skip = 0
        while True:
            value = self.log(
                count=1, pretty='%H %B', skip=skip).split(' ', 1)

            if not value[0]:
                break

            commit_hash, commit_msg = value
            yield commit_hash, commit_msg

            skip += 1

    def _last_upstream_commit_hash(self):
        """Return the hash for the last upstream commit in the repo.

        We use this to annotate patch-repo commits with the version of the
        working-repo they were based off of.

        The patch before the earliest Ply-Patch annotated commit is the last
        upstream commit.
        """
        applied = list(self._applied_patches())
        if not applied:
            return None

        last_applied_hash = applied[-1][0]
        return self.log(cmd_arg='%s^' % last_applied_hash,
                        count=1, pretty='%H').strip()

    def _applied_patches(self):
        """Return a list of patches that have already been applied to this
        branch.

        We figure this out by walking backwards from HEAD until we reach a
        commit without a 'Ply-Patch' commit msg annotation.
        """
        for commit_hash, commit_msg in self._walk_commit_msgs_backwards():
            patch_name = utils.get_patch_annotation(commit_msg)
            if not patch_name:
                break
            yield commit_hash, patch_name

    def _store_patch_files(self, patch_names, filenames,
                           parent_patch_name=None):
        """Store a set of patch files in the patch-repo."""
        for patch_name, filename in zip(patch_names, filenames):
            # Ensure destination exists (in case a prefix was supplied)
            dirname = os.path.dirname(patch_name)
            dest_path = os.path.join(self.patch_repo.path, dirname)
            if dirname and not os.path.exists(dest_path):
                os.makedirs(dest_path)

            shutil.move(os.path.join(self.path, filename),
                        os.path.join(self.patch_repo.path, patch_name))

        return self.patch_repo.add_patches(
            patch_names, parent_patch_name=parent_patch_name)

    def _commit_to_patch_repo(self, commit_msg):
        if not self.patch_repo.uncommitted_changes():
            return

        based_on = self._last_upstream_commit_hash()
        commit_msg += '\n\nPly-Based-On: %s' % based_on
        self.patch_repo.commit(commit_msg)

    @property
    def patch_repo_path(self):
        try:
            path = self.config('get', config_key='ply.patchrepo')[0]
        except git.exc.GitException:
            path = None
        return path

    @property
    def patch_repo(self):
        """Return a patch repo object associated with this working repo via
        the ply.patchrepo git config.
        """
        if not self.patch_repo_path:
            raise exc.NoLinkedPatchRepo

        return PatchRepo(self.patch_repo_path,
                         quiet=self.quiet,
                         supress_warnings=self.supress_warnings)

    @property
    def _patch_conflict_path(self):
        return os.path.join(self.path, '.patch-conflict')

    def _create_conflict_file(self, patch_name):
        """The conflict-file gives us a way to memorize the patch-name of the
        conflicting patch so that we can apply the patch-annotation after the
        user resolves the conflict.
        """
        with open(self._patch_conflict_path, 'w') as f:
            f.write('%s\n' % patch_name)

    def _resolve_conflict(self, method):
        """Resolve a conflict using one of the following methods:

            1. Abort
            2. Skip
            3. Resolve
        """
        if not os.path.exists(self._patch_conflict_path):
            raise exc.PathNotFound

        kwargs = {method: True}
        self.am(**kwargs)

        with open(self._patch_conflict_path) as f:
            patch_name = f.read().strip()

        os.unlink(self._patch_conflict_path)
        return patch_name

    def abort(self):
        """Abort a failed merge.

        NOTE: this doesn't rollback commits that have successfully applied.
        """
        self._resolve_conflict('abort')
        os.unlink(self._restore_stats_path)

        # Throw away any conflict resolution changes
        self.reset('HEAD', hard=True)

    def link(self, patch_repo_path):
        """Link a working-repo to a patch-repo."""
        if self.patch_repo_path:
            raise exc.AlreadyLinkedToPatchRepo

        self.config('add', config_key='ply.patchrepo',
                    config_value=patch_repo_path)

    def unlink(self):
        """Unlink a working-repo from a patch-repo."""
        if not self.patch_repo_path:
            raise exc.NoLinkedPatchRepo

        self.config('unset', config_key='ply.patchrepo')

    def skip(self):
        """Skip applying current patch and remove from the patch-repo.

        This is useful if the patch is no longer relevant because a similar
        change was made upstream.
        """
        patch_name = self._resolve_conflict('skip')
        self.patch_repo.remove_patches([patch_name])
        self.restore()  # Apply remaining patches

    def resolve(self):
        """Resolves a commit and refreshes the affected patch in the
        patch-repo.

        Rather than generate a new commit in the patch-repo for each refreshed
        patch, which would make for a rather chatty history, we instead commit
        one time after all of the patches have been applied.
        """
        patch_name = self._resolve_conflict('resolved')

        filenames, parent_patch_name = self._create_patches('HEAD^')

        self._store_patch_files([patch_name], filenames,
                                parent_patch_name=parent_patch_name)

        self._add_patch_annotation(patch_name)
        self.restore()  # Apply remaining patches

    @property
    def _restore_stats_path(self):
        return os.path.join(self.path, '.restore-stats')

    def _update_restore_stats(self, delta_updated=0, delta_removed=0):
        """Restore-Stats allows us to craft a more useful commit message,
        containing the number of patches updated and removed during a restore.

        The data is stored in a temp file that is cleared when the restore is
        either finished or aborted.

        Diffstat was not used because it is line-oriented as opposed to
        file-oriented. Moreover, a `git log --stat` would provide that info
        already, so the commit msg would be duplicating info.
        """
        updated, removed = self._get_restore_stats()

        updated += delta_updated
        removed += delta_removed

        with open(self._restore_stats_path, 'w') as f:
            f.write('%d %d\n' % (updated, removed))

    def _get_restore_stats(self):
        updated = 0
        removed = 0

        if os.path.exists(self._restore_stats_path):
            with open(self._restore_stats_path, 'r') as f:
                updated, removed = map(int, f.read().strip().split(' '))

        return updated, removed

    def restore(self, three_way_merge=True, commit_msg=None):
        """Applies a series of patches to the working repo's current
        branch.
        """
        #####################################################################
        #
        #                          Reentrant-Section
        #
        # This bit of code is called repeatedly until all of the patches have
        # been successfully applied, skipped, or we've aborted the restore.
        #
        #####################################################################
        if self.uncommitted_changes():
            raise exc.UncommittedChanges

        applied = set(pn for _, pn in self._applied_patches())

        for patch_name in self.patch_repo.series:
            if patch_name in applied:
                continue

            patch_path = os.path.join(self.patch_repo.path, patch_name)

            # Apply from mbox formatted patch, three possible outcomes here:
            #
            # 1. Patch applies cleanly: move on to next patch
            #
            # 2. Patch has conflicts: capture state, bail so user can fix
            #    conflicts
            #
            # 3. Patch was already applied: remove from patch-repo, move on to
            #    next patch
            try:
                self.am(patch_path, three_way_merge=three_way_merge)
            except git.exc.PatchDidNotApplyCleanly:
                # Memorize the patch-name that caused the conflict so that
                # when we later resolve it, we can add the patch-annotation
                self._create_conflict_file(patch_name)
                self._update_restore_stats(delta_updated=1)
                raise
            except git.exc.PatchAlreadyApplied:
                self.patch_repo.remove_patches([patch_name])
                self.warn("Patch '%s' appears to be upstream, removing from"
                          " patch-repo" % patch_name)
                self._update_restore_stats(delta_removed=1)
            else:
                self._add_patch_annotation(patch_name)

        ######################################################################
        #
        #                              Endgame
        #
        # This bit of code is only reached after all patches have been applied
        # sucessfully or have been skipped. To finish up, we commit any
        # changes we're holding in the patch-repo and peform any necessary
        # housekeeping (removing tempfiles, etc.)
        #
        ######################################################################
        updated, removed = self._get_restore_stats()
        if os.path.exists(self._restore_stats_path):
            os.unlink(self._restore_stats_path)

        if not commit_msg:
            commit_msg = 'Refreshing patches: %d updated, %d removed' % (
                updated, removed)

        self._commit_to_patch_repo(commit_msg)

    def rollback(self):
        """Rollback to that last upstream commit."""
        if self.uncommitted_changes():
            raise exc.UncommittedChanges

        based_on = self._last_upstream_commit_hash()
        if based_on:
            self.reset(based_on, hard=True)
        else:
            raise exc.NoPatchesApplied

    def _create_patches(self, since):
        """
        The default output of format-patch isn't ideally suited for our
        purposes since it contains extraneous info as well as text that
        changes even if the underlying patch doesn't change.

        The following options are used to correct this:

        --keep-subject - remove unecessary [PATCH] prefix
        --no-stat - remove unecessary diffstat
        --no-numbered - remove number of patches in set from subject line

        In addition, we need to rewrite the first-line of the patch-file to
        remove an unecessary commit-hash. On refresh, this would change even
        if the actual patch was the same, leading to very noisy diffs.
        """
        filenames = self.format_patch(
            since, keep_subject=True, no_numbered=True, no_stat=True)

        # Rewrite first-line 'From <commit-hash>' -> 'From ply'
        for filename in filenames:
            from_filename = os.path.join(self.path, filename)
            with tempfile.NamedTemporaryFile(delete=False) as to_file:
                with open(from_filename) as from_file:
                    # len("From fee0d7191da38033ffb29e1d6d88892862064943") =
                    # 45
                    first_line = "From ply%s" % from_file.readline()[45:]
                    to_file.write(first_line)
                    shutil.copyfileobj(from_file, to_file)
            shutil.move(to_file.name, from_filename)

        commit_msg = self.log(since, pretty='%B', count=1)
        parent_patch_name = utils.get_patch_annotation(commit_msg)
        return filenames, parent_patch_name

    def save(self, since, prefix=None):
        """Save a series of commits as patches into the patch-repo."""
        if self.uncommitted_changes() or self.patch_repo.uncommitted_changes():
            raise exc.UncommittedChanges

        if '..' in since:
            raise ValueError(".. not supported at the moment")

        filenames, parent_patch_name = self._create_patches(since)

        patch_names = []
        for filename in filenames:
            # If commit already has annotation, use that patch-name
            with open(os.path.join(self.path, filename)) as f:
                patch_name = utils.get_patch_annotation(f.read())

            # Otherwise... take it from the `git format-patch` filename
            if not patch_name:
                # Strip 0001- prefix that git format-patch provides. Like
                # `quilt`, `ply` uses a `series` for patch ordering.
                patch_name = filename.split('-', 1)[1]

                # Add our own subdirectory prefix, if needed
                if prefix:
                    patch_name = os.path.join(prefix, patch_name)

            patch_names.append(patch_name)

        added, updated = self._store_patch_files(
            patch_names, filenames, parent_patch_name=parent_patch_name)

        # Remove vestigial patches (anything in the series file after the last
        # recognized patch name)
        series = self.patch_repo.series
        last_idx = series.index(patch_names[-1])
        vestigial = series[last_idx + 1:len(series)]
        removed = self.patch_repo.remove_patches(vestigial)

        # Rollback and reapply patches so that working repo has
        # patch-annotations for latest saved patches
        num_patches = len(self.patch_repo.series)
        self.reset('HEAD~%d' % num_patches, hard=True)

        commit_msg = "Saving patches: added %d, updated %d, removed %d" % (
            len(added), len(updated), len(removed))

        # We have to commit to the patch-repo AFTER rolling-back and
        # reapplying so that we have the patch-annotations necessary to figure
        # out the correct Ply-Based-On annotation in the patch-repo.
        self.restore(commit_msg=commit_msg)

    @property
    def status(self):
        """Return the status of the working-repo."""
        if os.path.exists(self._patch_conflict_path):
            return 'restore-in-progress'

        if len(list(self._applied_patches())) == 0:
            return 'no-patches-applied'

        return 'all-patches-applied'

    def check_patch_repo(self):
        return self.patch_repo.check()


class PatchRepo(git.Repo):
    """Represents a git repo containing versioned patch files."""

    def check(self):
        """Sanity check the patch-repo.

        This ensures that the number of patches in the patch-repo matches the
        series file.
        """
        series = set(self.series)
        patch_names = set(self.patch_names)

        # Has entry in series file but not actually present
        no_file = series - patch_names

        # Patch files exists, but no entry in series file
        no_series_entry = patch_names - series

        if not no_file and not no_series_entry:
            return ('ok', {})

        return ('failed', dict(no_file=no_file,
                               no_series_entry=no_series_entry))

    @property
    def patch_names(self):
        """Return all patch files in the patch-repo (recursively)."""
        patch_names = []
        # Strip base path so that we end up with relative paths against the
        # patch-repo making the results `patch_names`
        strip = self.path + '/'
        for path in utils.recursive_glob(self.path, '*.patch'):
            patch_names.append(path.replace(strip, ''))
        return patch_names

    @contextlib.contextmanager
    def _mutate_series_file(self):
        """The series file is effectively a list of patches to apply in order.
        This function allows you to add/remove/reorder the patches in the
        series-file by manipulating a plain-old Python list.
        """
        # FIXME: mutate_series_file doesn't support recursive series files yet
        # Read in series file and create list
        patch_names = self._non_recursive_series(self.series_path)

        # Allow caller to mutate it
        yield patch_names

        # Write back new contents
        with open(self.series_path, 'w') as f:
            for patch_name in patch_names:
                f.write('%s\n' % patch_name)

        self.add('series')

    def add_patches(self, patch_names, parent_patch_name=None):
        """Add patches to the patch-repo, including add them to the series
        file in the appropriate location.


        `parent_patch_name` represents where in the `series` file we should
        insert the new patch set.

        `None` indicates that the patch-set doesn't have a parent so it should
        be inserted at the beginning of the series file.
        """
        added = set()
        updated = set()
        with self._mutate_series_file() as entries:
            if parent_patch_name:
                base = entries.index(parent_patch_name) + 1
            else:
                base = 0

            for idx, patch_name in enumerate(patch_names):
                self.add(patch_name)

                if patch_name in entries:
                    # Already exists, reorder patch by removing it from
                    # current location and inserting it into the new location.
                    entries.remove(patch_name)
                    updated.add(patch_name)
                else:
                    added.add(patch_name)

                entries.insert(base + idx, patch_name)

        return added, updated

    def remove_patches(self, patch_names):
        removed = set()
        with self._mutate_series_file() as entries:
            for patch_name in patch_names:
                self.rm(patch_name)
                entries.remove(patch_name)
                removed.add(patch_name)
        return removed

    def initialize(self):
        """Initialize the patch repo (create series file and git-init)."""
        self.init(self.path)

        if not os.path.exists(self.series_path):
            with open(self.series_path, 'w') as f:
                pass

            self.add('series')
            self.commit('Ply init')

    @property
    def series_path(self):
        return os.path.join(self.path, 'series')

    def _non_recursive_series(self, series_path):
        patch_names = []

        with open(series_path) as f:
            for line in f:
                line = line.strip()

                if not line:
                    continue

                patch_names.append(line)

        return patch_names

    def _recursive_series(self, series_path):
        """Emit patch_names from series file, handling -i recursion."""
        for patch_name in self._non_recursive_series(series_path):
            if not patch_name.startswith('-i '):
                yield patch_name
                continue

            # If entry starts with -i, what follows is a path to a
            # child series file
            series_rel_path = patch_name.split(' ', 1)[1].strip()
            child_series_path = os.path.join(
                self.path, series_rel_path)
            patch_dir = os.path.dirname(series_rel_path)
            for child_patch_name in self._recursive_series(
                    child_series_path):
                yield os.path.join(patch_dir, child_patch_name)

    @property
    def series(self):
        return list(self._recursive_series(self.series_path))

    def _changed_files_for_patch(self, patch_name):
        """Returns a set of files that were modified by specified patch."""
        changed_files = set()
        patch_path = os.path.join(self.path, patch_name)
        with open(patch_path) as f:
            for line in f:
                line = line.strip()
                if line.startswith('--- a/'):
                    line = line.replace('--- a/', '')
                elif line.startswith('+++ b/'):
                    line = line.replace('+++ b/', '')
                else:
                    continue
                filename = line
                if filename.startswith('/dev/null'):
                    continue
                changed_files.add(filename)

        return changed_files

    def _changes_by_filename(self):
        """Return a breakdown of what patches modifiied a given file over the
        whole patch series.

        {filename: [patch1, patch2, ...]}
        """
        file_changes = collections.defaultdict(list)
        for patch_name in self.series:
            changed_files = self._changed_files_for_patch(patch_name)
            for filename in changed_files:
                file_changes[filename].append(patch_name)

        return file_changes

    def patch_dependencies(self):
        """Returns a graph representing the file-dependencies between patches.

        To retiterate, this is not a call-graph representing
        code-dependencies, this is a graph representing how files change
        between patches, useful in breaking up a large patch set into smaller,
        independent patch sets.

        The graph uses patch_names as nodes with directed edges representing
        files that both patches modify. In Python:

            {(dependent, parent): set(file_both_touch1, file_both_touch2, ...)}
        """
        graph = collections.defaultdict(set)
        for filename, patch_names in self._changes_by_filename().iteritems():
            parent = None
            for dependent in patch_names:
                if parent:
                    graph[(dependent, parent)].add(filename)
                parent = dependent
        return graph

    def patch_dependency_dot_graph(self):
        """Return a DOT version of the dependency graph."""
        lines = ['digraph patchdeps {']

        for (dependent, parent), changed_files in\
                self.patch_dependencies().iteritems():
            label = ', '.join(sorted(changed_files))
            lines.append('"%s" -> "%s" [label="%s"];' % (
                dependent, parent, label))

        lines.append('}')
        return '\n'.join(lines)
