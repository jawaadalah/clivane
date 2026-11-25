# mirror-ls

Mirror wrapper for the existing `ls` CLI (wrapped by clivane).
The wrapper forwards arguments to the real tool; `--json` returns a
structured envelope `{tool, argv, exit_code, stdout, stderr}`.

## Command map (from `ls --help`)

- (no subcommands detected; single-command tool)

## Top-level help

```
Usage: ls [OPTION]... [FILE]...
List information about the FILEs (the current directory by default).
Sort entries alphabetically if none of -cftuvSUX nor --sort is specified.

Mandatory arguments to long options are mandatory for short options too.
  -a, --all                  do not ignore entries starting with .
  -A, --almost-all           do not list implied . and ..
      --author               with -l, print the author of each file
  -b, --escape               print C-style escapes for nongraphic characters
      --block-size=SIZE      with -l, scale sizes by SIZE when printing them;
                             e.g., '--block-size=M'; see SIZE format below

  -B, --ignore-backups       do not list implied entries ending with ~
  -c                         with -lt: sort by, and show, ctime (time of last
                             change of file status information);
                             with -l: show ctime and sort by name;
                             otherwise: sort by ctime, newest first

  -C                         list entries by columns
      --color[=WHEN]         color the output WHEN; more info below
  -d, --directory            list directories themselves, not their contents
  -D, --dired                generate output designed for Emacs' dired mode
  -f                         list all entries in directory order
  -F, --classify[=WHEN]      append indicator (one of */=>@|) to entries WHEN
      --file-type            likewise, except do not append '*'
      --format=WORD          across -x, commas -m, horizontal -x, long -l,
                             single-column -1, verbose -l, vertical -C

      --full-time            like -l --time-style=full-iso
  -g                         like -l, but do not list owner
      --group-directories-first
                             group directories before files;
                             can be augmented with a --sort option, but any
                             use of --sort=none (-U) disables
```
