"""macOS Quick Action: archive mail straight from the Finder's context menu.

Writes an Automator *service* bundle to ``~/Library/Services``. After that, a
right-click on one or more ``.eml`` files — or on whole folders — offers an
"Archive with imapArc" entry that runs :func:`imaparc.adhoc.run_adhoc` over the
selection.

The bundle is two property lists in a fixed directory layout; it is generated
here rather than shipped as a binary blob, so the executable path is baked in at
install time and the whole thing stays reviewable as code.
"""

from __future__ import annotations

import plistlib
import shutil
from pathlib import Path

from imaparc.config import REQUIRED_TOOLS
from imaparc.exceptions import ImapArcError

SERVICE_NAME = "Mit imapArc archivieren"

# Where gs/qpdf/verapdf usually live, appended in case one of them is not
# installed yet when the action is created. Without these, a tool added later
# would still be invisible to the action.
_FALLBACK_TOOL_DIRS = (
    "/opt/homebrew/bin",  # Homebrew on Apple silicon
    "/usr/local/bin",  # Homebrew on Intel
    str(Path.home() / "verapdf"),  # install.sh's veraPDF fallback location
)

# A Quick Action lives in the user's Services directory; macOS picks it up from
# there without any registration step.
SERVICES_DIR = Path.home() / "Library" / "Services"

# Values mirror what Automator itself writes for a "Run Shell Script" action.
_ACTION_BUNDLE = "/System/Library/Automator/Run Shell Script.action"
_INPUT_AS_ARGUMENTS = 1  # 0 would pipe the paths to stdin instead


def _info_plist() -> dict[str, object]:
    """The service registration Finder reads to build the context menu."""
    return {
        "NSServices": [
            {
                "NSMenuItem": {"default": SERVICE_NAME},
                "NSMessage": "runWorkflowAsService",
                "NSRequiredContext": {"NSApplicationIdentifier": "com.apple.finder"},
                # public.item covers files *and* directories, so a mixed
                # selection of .eml files and folders is offered the action.
                "NSSendFileTypes": ["public.item"],
            }
        ]
    }


def _document_wflow(command: str) -> dict[str, object]:
    """The Automator workflow itself: one "Run Shell Script" action."""
    return {
        "AMApplicationBuild": "521",
        "AMApplicationVersion": "2.10",
        # A *string*, not a number: Automator reads this key as text and dies
        # with "unrecognized selector objCType" when it is an integer.
        "AMDocumentVersion": "2",
        "actions": [
            {
                "action": {
                    "AMAccepts": {
                        "Container": "List",
                        "Optional": False,
                        "Types": ["com.apple.cocoa.path"],
                    },
                    "AMActionVersion": "2.0.3",
                    "AMApplication": ["Automator"],
                    "AMProvides": {
                        "Container": "List",
                        "Types": ["com.apple.cocoa.path"],
                    },
                    "ActionBundlePath": _ACTION_BUNDLE,
                    "ActionName": "Run Shell Script",
                    "ActionParameters": {
                        "COMMAND_STRING": command,
                        "CheckedForUserDefaultShell": True,
                        "inputMethod": _INPUT_AS_ARGUMENTS,
                        "shell": "/bin/bash",
                        "source": "",
                    },
                    "BundleIdentifier": "com.apple.RunShellScript",
                    "CFBundleVersion": "2.0.3",
                    "CanShowSelectedItemsWhenRun": False,
                    "CanShowWhenRun": True,
                    "Category": ["AMCategoryUtilities"],
                    "Class Name": "RunShellScriptAction",
                    "InputUUID": "00000000-0000-0000-0000-000000000001",
                    "Keywords": ["Shell"],
                    "OutputUUID": "00000000-0000-0000-0000-000000000002",
                    "UUID": "00000000-0000-0000-0000-000000000003",
                    "UnlocalizedApplications": ["Automator"],
                    "arguments": {},
                    "isViewVisible": 1,
                },
                "isViewVisible": 1,
            }
        ],
        "connectors": {},
        "workflowMetaData": {
            "serviceApplicationBundleID": "com.apple.finder",
            "serviceApplicationPath": "/System/Library/CoreServices/Finder.app",
            "serviceInputTypeIdentifier": "com.apple.Automator.fileSystemObject",
            "serviceOutputTypeIdentifier": "com.apple.Automator.nothing",
            "serviceProcessesInput": 0,
            "processesInput": 0,
            "presentationMode": 11,
            "systemImageName": "NSActionTemplate",
            "useAutomaticInputType": 0,
            "workflowTypeIdentifier": "com.apple.Automator.servicesMenu",
        },
    }


def _tool_dirs() -> list[str]:
    """Directories holding gs/qpdf/verapdf, resolved now, plus the usual spots.

    Order is preserved and duplicates dropped, so the resulting PATH stays short
    and deterministic.
    """
    found = [
        str(Path(path).parent)
        for tool in REQUIRED_TOOLS
        if (path := shutil.which(tool)) is not None
    ]
    ordered: list[str] = []
    for directory in [*found, *_FALLBACK_TOOL_DIRS]:
        if directory not in ordered:
            ordered.append(directory)
    return ordered


def _command(executable: Path, name: str | None) -> str:
    """The shell lines the action runs, with every selected path appended.

    Finder launches a Service through launchd with a bare PATH, so the
    directories of the external tools are baked in here — otherwise
    ``ToolPaths.resolve()`` finds nothing and the action fails with "Missing
    required tool(s): gs, qpdf, verapdf" even though a terminal run works.
    The inherited ``$PATH`` is kept last so nothing is shadowed.

    ``"$@"`` forwards the whole Finder selection, which ``collect_eml`` already
    accepts as a mix of files and directories.
    """
    option = f" --name {name}" if name else ""
    path_prefix = ":".join(_tool_dirs())
    return f'export PATH="{path_prefix}:$PATH"\n"{executable}" eml{option} "$@"'


def install_quick_action(
    services_dir: Path = SERVICES_DIR,
    *,
    executable: Path,
    name: str | None = None,
) -> Path:
    """Write the Quick Action bundle and return its path.

    Args:
        services_dir: Where to install; the default is picked up by macOS.
        executable: Absolute path to the ``imaparc`` binary. It must be absolute
            because a Service does **not** inherit the user's ``PATH`` — a bare
            command name would silently fail to resolve when Finder runs it.
        name: Optional ``--name`` value baked into the command.

    Returns:
        The path of the installed ``.workflow`` bundle.

    Raises:
        ImapArcError: If ``executable`` is not an absolute path.
    """
    if not executable.is_absolute():
        raise ImapArcError(
            f"the imaparc path must be absolute (a macOS Service does not "
            f"inherit PATH): {executable}"
        )

    bundle = services_dir / f"{SERVICE_NAME}.workflow"
    contents = bundle / "Contents"
    # Replace wholesale: a leftover file from an older layout would otherwise
    # survive and could shadow the new definition.
    if bundle.exists():
        shutil.rmtree(bundle)
    contents.mkdir(parents=True)

    (contents / "Info.plist").write_bytes(plistlib.dumps(_info_plist()))
    (contents / "document.wflow").write_bytes(
        plistlib.dumps(_document_wflow(_command(executable, name)))
    )
    return bundle
