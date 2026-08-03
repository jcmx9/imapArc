"""File-manager action: archive mail straight from the context menu.

A right-click on one or more ``.eml`` files — or on whole folders — offers an
"Archive with imapArc" entry that runs :func:`imaparc.adhoc.run_adhoc` over the
selection. Two mechanisms, picked by :func:`install_action`:

* macOS: an Automator *service* bundle in ``~/Library/Services``, two property
  lists in a fixed directory layout.
* everything else: an XDG Desktop Entry in ``~/.local/share/applications``,
  which Nautilus, Dolphin and Thunar read for "Open With".

Both are generated here rather than shipped as blobs, so the executable path is
baked in at install time and the whole thing stays reviewable as code.
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


APPLICATIONS_DIR = Path.home() / ".local" / "share" / "applications"
DESKTOP_FILE = "imaparc-archive.desktop"


def install_desktop_action(
    applications_dir: Path = APPLICATIONS_DIR,
    *,
    executable: Path,
    name: str | None = None,
) -> Path:
    """Write a Desktop Entry so Linux file managers offer imapArc on a selection.

    The counterpart to the macOS Quick Action. A hidden ``.desktop`` entry
    declaring the MIME types it handles is what Nautilus, Dolphin and Thunar all
    read for "Open With" — there is no cross-desktop context-menu API, but this
    much every one of them honours.

    ``%F`` passes the whole selection; ``%f`` would hand over only the first item
    and quietly drop the rest. ``NoDisplay=true`` keeps it out of the application
    menu, since it is a file action rather than something to launch on its own.

    Args:
        applications_dir: Where to install; the default is on the XDG search path.
        executable: Absolute path to ``imaparc`` — a file manager does not
            resolve names against the user's shell ``PATH``.
        name: Optional ``--name`` value baked into the command.

    Returns:
        The path of the written ``.desktop`` file.

    Raises:
        ImapArcError: If ``executable`` is not an absolute path.
    """
    if not executable.is_absolute():
        raise ImapArcError(
            f"the imaparc path must be absolute (a file manager does not "
            f"inherit your shell PATH): {executable}"
        )
    applications_dir.mkdir(parents=True, exist_ok=True)
    option = f" --name {name}" if name else ""
    path = applications_dir / DESKTOP_FILE
    path.write_text(
        "[Desktop Entry]\n"
        "Type=Application\n"
        f"Name={SERVICE_NAME}\n"
        "Comment=Archive selected .eml files as PDF/A\n"
        f'Exec="{executable}" eml{option} %F\n'
        "MimeType=message/rfc822;inode/directory;\n"
        "NoDisplay=true\n"
        "Terminal=true\n",
        encoding="utf-8",
    )
    return path


def install_action(
    platform: str,
    *,
    executable: Path,
    name: str | None = None,
    services_dir: Path = SERVICES_DIR,
    applications_dir: Path = APPLICATIONS_DIR,
) -> Path:
    """Install the file-manager action fitting ``platform``; return its path.

    Takes the platform as an argument rather than reading ``sys.platform``
    itself, so both branches are reachable from a test without patching
    process-wide state — ``sys.platform`` is what ``sysconfig`` derives module
    names from, and overriding it breaks unrelated imports.

    Args:
        platform: A ``sys.platform`` value. Only macOS has its own mechanism;
            every other system gets the XDG Desktop Entry.
        executable: Absolute path to ``imaparc``.
        name: Optional ``--name`` value baked into the command.
        services_dir: Where a macOS Quick Action goes.
        applications_dir: Where a Desktop Entry goes.
    """
    if platform == "darwin":
        return install_quick_action(services_dir, executable=executable, name=name)
    return install_desktop_action(applications_dir, executable=executable, name=name)


def action_hint(platform: str) -> str:
    """How to reach the installed action, in the words of that platform's UI."""
    if platform == "darwin":
        return (
            f"Rechtsklick auf .eml-Dateien oder Ordner im Finder → Dienste → "
            f"„{SERVICE_NAME}“."
        )
    return (
        f"Rechtsklick auf .eml-Dateien oder Ordner → Öffnen mit → „{SERVICE_NAME}“. "
        "Manche Dateimanager wollen einmal neu gestartet werden."
    )


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
