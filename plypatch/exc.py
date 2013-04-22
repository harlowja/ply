class PlyException(Exception):
    pass


class AlreadyLinkedToPatchRepo(PlyException):
    pass


class NoLinkedPatchRepo(PlyException):
    pass


class NoPatchesApplied(PlyException):
    pass


class PathNotFound(PlyException):
    pass


class UncommittedChanges(PlyException):
    pass
