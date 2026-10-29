# netwatch — agent-scheduler (Hermes) deploy notes
#
# netwatch was written for this shape: a scheduler runs one process, and the
# process prints *only* when a human needs to know something. That maps onto an
# agent scheduler with no LLM in the loop ("no_agent" mode), so a run costs the
# pings and nothing else.
#
# Hermes:
#   hermes cron add \
#     --name "netwatch — network health" \
#     --schedule "*/5 * * * *" \
#     --script netwatch.py \
#     --no-agent \
#     --deliver telegram
#
# The script must live in ~/.hermes/scripts/ (or be referenced by absolute
# path) and read its config from ./netwatch.json or $NETWATCH_CONFIG.
#
# Whatever scheduler you use, the contract is the same:
#   * run it every 5 minutes
#   * treat empty stdout as "all well, say nothing"
#   * deliver non-empty stdout to a human
#   * keep data.json/state.json on persistent storage between runs
#
# state.json is what makes alerting quiet: it carries the last thing the human
# was told per target, the consecutive-bad-sample counter, and any MAC-resolved
# addresses. Delete it and the next run re-announces itself and re-alerts on
# whatever is currently bad.
