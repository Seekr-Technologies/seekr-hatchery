# Task: fix-kubectl

**Status**: in-progress
**Branch**: hatchery/fix-kubectl
**Created**: 2026-05-14 09:13

## Objective

We have a problem with the kubectl proxy:

1. It's certificate expires after 24 hours. Some tasks run longer. We should add a cert refresh mechanism.
2. This should be fixable by restarting the sandbox - the kubectl proxy should get recreated. However, the cert is still invalid

Please identify the root cause and fix

## Agreed Plan

*(To be filled in after planning discussion)*

## Progress Log

*(Steps will appear here once the plan is agreed)*

## Summary

*(Fill in on completion — then remove Agreed Plan and Progress Log above.
Cover: key decisions made, patterns established, files changed, gotchas,
and anything a future agent working in this repo should know.)*
