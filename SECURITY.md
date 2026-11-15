# Security policy

## Reporting a vulnerability

Please report privately through GitHub private vulnerability reporting: open the
[Security tab](https://github.com/83noit/mira-ble/security) of this repository and choose "Report a vulnerability".
Do not open a public issue for a vulnerability.

## Scope

This library drives hot water, so safety defects are in scope and are treated as security issues. In
particular:

- anything that could make the shower run hotter than the temperature that was asked for, or
  bypass the temperature limits;
- anything that reports water as stopped when it is still running.

## Supported versions

Only the latest release is supported. Fixes are released as a new version, not backported.

## Bounty

There is no bug bounty.
