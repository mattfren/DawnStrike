# Rollback rehearsal

Status: `PASS`

The immediate prior production deployment remained addressable:

- deployment: `dpl_3h12YiToYE5YKxH5YzX6BLyrMv7V`;
- source SHA: `52354f4a5bd3924d8921fa143089baab9cb7ba63`;
- build ID: `508912bf25a6f78b800b`;
- data hash:
  `17fe8732fc60499e7aa092f43e1627f81a53a1fc480d0890f9e6a25672e808b7`.

On 2026-07-30 all three production aliases were temporarily assigned to that
deployment. The public alias returned the exact prior source, build, and data
hashes.

In a `finally` restoration path, all three aliases were then returned to:

- deployment: `dpl_AbTsQJzj1EvMQ5Bd51naboU1BMv6`;
- source SHA: `51f79ff2a738110b486111d85c4d93cfda9f4ec8`;
- build ID: `5ef6a274f37fd1dbae87`;
- data hash:
  `3a134cc971e69a6f01a50c63687f9440883e82e833afa09bd120d319270cd56d`.

Final inspection proved all three aliases resolve to the final deployment.
No database, message, scanner, or broker action occurred during the rehearsal.

The prior deployment and legacy local operator fallback must remain available
through the required seven-market-day rollback window. Legacy deletion is not
authorized by this packet.
