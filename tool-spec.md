Spec: Are.na Group-to-ALL Auto-Mirror

1. Purpose
   Build a small automation that watches a group’s Are.na channels and, whenever a block is newly attached to any eligible source channel, also attaches that same block to a designated ALL channel.
   This is a polling-based sync, not a realtime webhook system.
2. Goal
   For a given Are.na group:

- automatically discover the group’s channels
- monitor them on a schedule
- detect blocks newly attached to those channels
- attach those blocks to ALL
- avoid duplicates
- avoid reprocessing old content

3. Non-goals
   This tool will not:

- be perfectly realtime
- mirror channels, only blocks
- backfill all historical content by default
- sync deletions/removals
- sync edits to blocks
- handle arbitrary external channels unless they are discoverable through the chosen group endpoint
- provide a full UI in the MVP

4. Assumptions
   These are the working assumptions behind the design:
1. Are.na exposes authenticated read/write API access for channels, blocks, and connections.
1. A block can belong to multiple channels.
1. Group contents can be discovered from a group endpoint that returns blocks and channels created by that group.
1. Channel contents include connection metadata such as connected_at.
1. The tool will run under one account/token that has permission to:
   _ read the relevant group channels
   _ attach blocks to the ALL channel
   If any of those assumptions are wrong in implementation, the write-path or discovery-path will need adjustment.
1. User story
   “Our group wants one master ALL channel. Whenever any block is added to any of our group’s channels, it should also appear in ALL, automatically, without us manually listing watched channels.”
1. High-level behavior
   On each scheduled run:
1. discover the group’s channels
1. exclude channels that should not be mirrored
1. fetch recent contents for each eligible source channel
1. detect blocks that are new since the last successful run
1. skip anything already mirrored
1. attach remaining blocks to ALL
1. persist updated state
1. Scope definition
   In scope

- group-based channel discovery
- scheduled polling
- new-block detection
- de-duplication
- persistent sync state
- basic logging
- GitHub Actions deployment
  Out of scope for MVP
- UI/dashboard
- per-channel configuration UI
- deletion propagation
- retry queues beyond simple retry/backoff
- notifications
- multi-group support
- OAuth for arbitrary users

8. Eligibility rules
   Source channels
   A source channel is eligible if:

- it is discoverable from the group discovery endpoint
- it is not the ALL channel
- it is not explicitly excluded by config
- the sync token has permission to read it
  Blocks
  A block is eligible if:
- it appears in a source channel’s contents
- it is newly connected relative to the saved checkpoint for that channel
- it has not already been mirrored to ALL

9. Core logic
   9.1 Channel discovery
   Each run will fetch group-created contents and filter for items of type Channel.
   Result:

- dynamic watched set
- no manual list required
  9.2 New-item detection
  For each source channel, the sync uses connected_at as the primary freshness signal.
  Reason:
- created_at is wrong for this use case
- an old block can be newly attached to a channel today
- connected_at tracks the channel attachment event
  9.3 De-duplication
  De-duplication will use block ID, not title or URL.
  A block should only be mirrored into ALL once, even if:
- it appears in multiple source channels
- the poller sees it again on a later run
- timestamps overlap at checkpoint boundaries
  9.4 State persistence
  The system must persist enough state to answer two questions:

1. “Have I already processed items in this source channel up to this point?”
2. “Have I already mirrored this block to ALL?”
3. State model
   Use a JSON state file in the MVP.
   Example:

{
"group_id": "my-group",
"all_channel_slug": "all",
"channels": {
"channel-a": {
"last_seen_connected_at": "2026-04-07T15:32:10Z",
"last_seen_ids_at_timestamp": ["123", "124"]
},
"channel-b": {
"last_seen_connected_at": "2026-04-07T15:29:00Z",
"last_seen_ids_at_timestamp": ["900"]
}
},
"mirrored_block_ids": [
"123",
"124",
"900"
]
}

Why both timestamp and IDs-at-timestamp?
Because timestamp alone is not fully safe.
If two blocks have the same connected_at, and a run stops halfway through, the next run could:

- skip one incorrectly, or
- reprocess one
  So the checkpoint per channel is:
- latest processed timestamp
- IDs processed at that exact timestamp
  That makes boundary handling robust.

11. Detailed sync algorithm
    Step 1: discover channels
    Fetch group contents, paginate as needed, filter to Channel.
    Step 2: exclude channels
    Remove:

- ALL
- any blocked/excluded channel slugs from config
  Step 3: for each source channel
  Fetch recent channel contents, newest first.
  Step 4: determine candidate blocks
  A block is a candidate if:
- connected_at is greater than the stored checkpoint timestamp, or
- connected_at equals the checkpoint timestamp but its ID is not in last_seen_ids_at_timestamp
  Step 5: filter duplicates
  Skip candidate if block ID is already in mirrored_block_ids.
  Step 6: mirror
  Attach the block to ALL.
  Step 7: commit progress
  Only after a successful write:
- add block ID to mirrored_block_ids
- update in-memory checkpoint candidate
  After all blocks in a channel are processed successfully:
- persist the channel checkpoint
  Step 8: save state
  Write updated JSON state.

12. First-run behavior
    The MVP should default to start now, not backfill history.
    That means on first run:

- discover channels
- fetch recent contents only to establish checkpoints
- do not mirror historical items
- only mirror blocks attached after setup
  Reason:
- avoids massive backfills
- avoids rate-limit pain
- avoids accidental flood of ALL
  Optional future mode:
- backfill_recent=true
- backfill only the first page or first N recent items per channel

13. Failure handling
    Write failure
    If attaching a block to ALL fails:

- log the error
- do not advance past that block as successfully processed
- continue or stop depending on error type
  Suggested behavior:
- retry transient failures
- stop on auth/config failures
  Crash during run
  If the run crashes midway:
- already-written blocks remain in mirrored_block_ids if state has been flushed
- if state has not been flushed, duplicates are still partly prevented by checking ALL and by later retry
  Partial channel completion
  Do not update the channel checkpoint until processing for that channel is complete.

14. Rate limiting
    The sync should assume rate limits exist and behave conservatively.
    Mitigations:

- poll every 5 minutes, not every minute
- fetch only recent pages
- stop scanning a channel once items are older than the checkpoint
- use small delays between writes if needed
- implement backoff on 429

15. Permissions and auth
    Auth method
    Use a personal access token for the group’s internal tool.
    Required capabilities
    The token’s account must be able to:

- read the group’s channels
- read channel contents
- attach blocks to ALL
  Secret storage
  In GitHub Actions:
- store token as a repository secret
- never commit it to the repo

16. Deployment target
    GitHub Actions
    This job will run on a schedule.
    Why GitHub Actions

- easy setup
- no always-on machine required
- good enough for polling every 5 minutes
- cheap or free depending on repo/plan
  Workflow shape
- scheduled trigger
- optional manual trigger
- single Python job
- concurrency control to avoid overlapping runs

17. Repository layout

arena-all-sync/
README.md
requirements.txt
sync.py
state.json # optional if committed approach is used
config.example.json
.github/
workflows/
sync.yml

Alternative:

- keep state.json out of git and store elsewhere

18. Config
    Example config:

{
"group_id": "my-group-slug",
"all_channel_slug": "all",
"excluded_channels": [
"all",
"archive"
],
"poll_recent_pages_per_channel": 2,
"group_discovery_pages": 5,
"dry_run": false
}

19. Logging requirements
    Each run should log:

- run start time
- number of discovered channels
- channels skipped
- number of candidate blocks found
- number of blocks mirrored
- duplicates skipped
- API errors
- run end status
  Log format can be plain text in MVP.

20. Idempotency requirements
    The sync must be safe to run repeatedly.
    That means:

- repeated runs should not create duplicate copies in ALL
- overlapping runs should not corrupt state
- retrying a previously successful block should be harmless or skipped

21. Concurrency requirements
    Only one sync run should operate at a time.
    In GitHub Actions:

- use workflow/job concurrency so one run cancels or queues behind another
  Reason:
- prevents two runs from racing on the same checkpoints/state

22. Edge cases
    Same block added to multiple channels
    Expected behavior:

- add to ALL once only
  New channel created in the group
  Expected behavior:
- discovered automatically on next run
  Channel removed or inaccessible
  Expected behavior:
- log and skip
- optionally remove stale checkpoint after repeated failures
  Block already manually added to ALL
  Expected behavior:
- skip once detected as already mirrored
  ALL channel included in discovery
  Expected behavior:
- exclude it explicitly
  Large group with many channels
  Expected behavior:
- only recent-page polling
- no full historical scan on each run

23. MVP success criteria
    The MVP is successful if:
1. it runs on GitHub Actions every 5 minutes
1. it discovers group channels automatically
1. when a new block is attached to any eligible channel, it gets attached to ALL
1. the same block is not added twice
1. rerunning does not create duplicates
1. state persists across runs
1. Nice-to-have later

- backfill mode
- admin dashboard
- Slack/Discord notifications
- per-channel include/exclude rules in config
- support for multiple groups
- healthcheck status file
- metrics
- SQLite instead of JSON
- manual resync command
- dry-run report mode
- web UI

25. Open questions
    These need confirmation during implementation:
1. What is the exact v3 endpoint for attaching an existing block to a channel?
1. What is the cleanest way to confirm a block is already in ALL?
   - local mirrored set only
   - or compare against ALL contents
1. Does the group discovery endpoint fully cover the channel universe you care about?
1. Are there any permissions quirks for private group channels?
1. How should state persist in GitHub?
   - commit state file back to repo
   - GitHub artifact/cache
   - external store
1. Recommended implementation plan
   Phase 1: prove the API

- authenticate
- list group-created channels
- read one channel’s recent contents
- attach one known block to ALL
  Phase 2: local prototype
- implement JSON state
- implement checkpoint logic
- implement dedupe
- run locally in dry-run mode
  Phase 3: GitHub Actions deployment
- add scheduled workflow
- add secrets
- add concurrency control
- verify state persistence
  Phase 4: hardening
- retries/backoff
- better logs
- excluded channels
- stale-channel handling

27. Short version
    This is a small scheduled sync service.
    It needs to:

- discover group channels
- poll for newly connected blocks
- dedupe by block ID
- mirror into ALL
- remember checkpoints
  The MVP is straightforward. The annoying parts are:
- exact write endpoint
- persistent state on GitHub Actions
- rate-limit-safe incremental polling
