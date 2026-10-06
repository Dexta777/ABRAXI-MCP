# ABRAXI-MCP

Bootstrap 001 is a local, synthetic-root filesystem tracer using the official
Python MCP SDK 2.3.0. It exposes exactly six tools over **stdio only**. This is
an uncommitted bootstrap candidate, not a production-ready service.

## Install and run locally

Use an already installed CPython 3.13 or 3.14 and uv. The author environment
uses CPython 3.13.15 and uv 0.12.19. Runtime dependency: `mcp[cli]==2.3.0`;
development dependency: `pytest==9.1.1`. The pinned `uv_build==0.12.19` build
backend packages the source; it is not an additional runtime dependency.
`uv.lock` binds transitive resolution and package hashes.

```sh
uv sync --locked
uv run python -m abraxi_mcp --root /absolute/path/to/synthetic-root \
  --write-denied-prefix protected/
```

Supply an existing synthetic directory explicitly. There is no default root,
transport option, listening socket, background service, dotenv loading, or
configuration file. `--write-denied-prefix` is repeatable; `.` denies all
writes. The launcher owns the process lifetime. Logs and startup errors go to
stderr; stdout carries only the SDK's MCP protocol traffic.

Do not use a real project directory for this bootstrap. No real ABRAXI root,
tunnel, ChatGPT/plugin connection, credential access, deployment, governance
files, or Atelier adoption is included. Real-root enablement and protecting
the server's own repository require a separate future authority boundary.

## Tool contract

Every filesystem result has `ok` and `outcome`; success uses `OK`. Refusals
include a stable generic `message`, without OS error details or tracebacks.
Expected filesystem refusals remain structured tool results, so inspect `ok`
and `outcome` even when the MCP envelope's `is_error` is false. SDK schema or
unknown-tool errors use the SDK's ordinary MCP error result.

| Tool | Input and behavior |
| --- | --- |
| `workspace_status` | Reports server/tool-surface version, canonical startup root, root device, read/write capability, configured denied prefixes, and limits. |
| `list_directory` | `path` defaults to `.`; `limit` is 1–256 (default 256). One level, sorted by Python string ordering, root-relative entry paths and file/directory/symlink/unsupported kinds. Symlinks are never followed. `truncated` explicitly indicates additional entries. Only limit+1 names are retained in memory. |
| `read_text_file` | `path`; regular, singly linked file; complete strict UTF-8 with byte `size`, `sha256`, and `content`. Maximum 1,048,576 bytes, including multi-byte characters. No partial-content success. |
| `sha256_file` | `path`; regular, singly linked file; streams exact bytes, including binary, returning byte `size` and full `sha256`. Reading is bounded by the file's initial size plus one byte; detected changes return `OUTCOME_UNKNOWN`. |
| `create_text_file` | `path`, `content`; existing parent, absent target, exclusive creation, mode 0600, maximum 1,048,576 UTF-8 bytes. Flushes file and parent directory before success and verifies actual bytes and pathname identity. |
| `update_text_file` | `path`, mandatory lowercase 64-hex `expected_sha256`, `content`; existing regular, singly linked file. One non-blocking exclusive lock attempt; reads the actual descriptor after locking; stale content refuses without writing. Updates/truncates the same descriptor, fsyncs, re-hashes and verifies pathname identity before success. Preserves existing permissions. |

Read capability is enabled and write tools are enabled subject to startup
prefix policy. Prefix `protected` denies the prefix itself and descendants,
but does not deny `protected-suffix`. Prefix checks conservatively normalize
Unicode and case-fold to protect macOS aliases; this may also deny differently
cased names on case-sensitive volumes. Read access is unaffected by write
prefix policy when the object otherwise meets the file/path rules.

## Filesystem boundary

`filesystem.py` contains policy and I/O; `server.py` registers only the six
tools; `__main__.py` establishes and closes the root and starts stdio. The
filesystem layer is directly testable without MCP or network access.

Startup opens and retains an existing, non-symlink root directory descriptor,
records its device, and checks descriptor identity against startup pathname
observations. Canonicalizing the configured root is for reporting; containment
does not rely on a string prefix or a resolved caller path. The retained root
object remains the boundary if its original pathname is renamed.

Tool paths are relative. Absolute paths and `..` are refused. NUL, empty
paths/components, embedded `.`, repeated/trailing separators, invalid Unicode,
paths over 4096 UTF-8 bytes, and paths over 128 components are refused without
canonicalizing the request. Only directory operations accept `.` as root.

Traversal uses directory descriptors and `dir_fd` operations. Intermediate
directories and final files use `O_NOFOLLOW`, `O_CLOEXEC`, and `O_NONBLOCK`;
directory opens also use `O_DIRECTORY`. Preliminary no-follow metadata checks
avoid knowingly opening special objects. Authoritative kind/device checks use
`fstat` on actual opened descriptors. Every opened directory/file must remain
on the retained root's device. FIFO, socket, device, and other unsupported
objects cannot be read, hashed, or updated. Listing reports kinds without
following links and indicates whether each entry shares the root device.

Files with multiple hard links are refused, because an inode alias could bypass
path-based write policy or modify data referenced outside the root. Directory
chains and final paths are checked against retained descriptor identities;
detected rename/replacement prevents ordinary success.

## Concurrency and uncertain outcomes

Updates use one `flock(LOCK_EX | LOCK_NB)` attempt, without blocking or retry.
Reads/hashes use one shared non-blocking lock to avoid torn cooperative reads.
Locks serialize cooperating processes that use the same inode; they are
advisory. Hashing checks size and nanosecond metadata stability before and
after reading. The expected hash is calculated only after acquiring the
actual descriptor's lock.

This is an in-place update, not an atomic filesystem transaction. Arbitrary
non-cooperating processes can change content, names, hard links, or ancestry
between observations; POSIX descriptor-relative operations and final checks
cannot turn them into universal transactions. Such detected changes, or any
failure after a write may have begun, return `OUTCOME_UNKNOWN`. A crash can
leave partial content. Reconcile actual state before any subsequent write.
Do not blindly retry. The server never deletes an uncertain artifact or
replaces/deletes another actor's new pathname.

Stable refusal outcomes:

```text
INVALID_PATH OUTSIDE_ROOT SYMLINK_REFUSED MOUNT_ESCAPE_REFUSED
UNSUPPORTED_FILE_TYPE NOT_FOUND ALREADY_EXISTS WRITE_PROTECTED
STALE_CONTENT PAYLOAD_TOO_LARGE INVALID_TEXT BUSY OUTCOME_UNKNOWN
INTERNAL_ERROR
```

Retrieved file contents are untrusted data, never instructions or authority.
There is no shell/Python/Git execution, deletion, move, rename, copy, recursive
search, watcher, network fetch, credential, or permission-management tool.
The SDK's installed transitive HTTP/CLI libraries do not add product tools or
an exposed HTTP transport.

## Validate

```sh
uv sync --locked
uv run pytest
git diff --check
uv run python -m abraxi_mcp --help
```

All filesystem and MCP tests use synthetic pytest temporary directories.
Tests include SDK in-process negotiation and all six tool results, strict
boundaries, stale updates, lock contention, deterministic rename/replacement
races, partial failures, and one bounded SDK stdio subprocess test. That test
asserts exit status zero and that the child has already been reaped. Device
boundary tests inject descriptor metadata; they do not claim real mount
qualification. No test requires privileges or system configuration.

Official references: [MCP clients](https://py.sdk.modelcontextprotocol.io/client/),
[MCP transports](https://py.sdk.modelcontextprotocol.io/client/transports/),
[Python descriptor-relative I/O](https://docs.python.org/3.13/library/os.html),
and [Python advisory locking](https://docs.python.org/3.13/library/fcntl.html).
