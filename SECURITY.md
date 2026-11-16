# Security policy

## Reporting a problem

Please report it privately, through GitHub's private vulnerability reporting: open the
[Security tab](https://github.com/83noit/mira-ble/security) of this repository and choose "Report a
vulnerability". Please don't open a public issue for it.

## What counts

This library turns hot water on and off, so safety bugs count as security bugs. In
particular:

- anything that could make the shower run hotter than asked, or get round the temperature
  limits;
- anything that says the water has stopped when it's still running.

## Supported versions

Only the latest release. Fixes come out as a new version; older versions aren't patched.

## Bounty

There's no bug bounty.
