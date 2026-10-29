#!/usr/bin/env bash
# Run by the repository owner with their own authenticated GitHub CLI.
# Replaces classic main protection with the checked-in policy; no deployments.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
repository=Kirito63884/ST-9economic-bot
command -v gh >/dev/null || { echo 'Install GitHub CLI (gh) first.'; exit 1; }

# Fail before modifying protection if Actions is disabled or API access is absent.
enabled=$(gh api "repos/$repository/actions/permissions" --jq '.enabled')
if [[ "$enabled" != true ]]; then
    echo 'Enable GitHub Actions in repository Settings → Actions before continuing.'
    exit 1
fi
echo "Applying classic main protection to $repository from .github/branch-protection.json"
gh api --method PUT "repos/$repository/branches/main/protection" \
    --input .github/branch-protection.json \
    --jq '{reviews: .required_pull_request_reviews.required_approving_review_count, checks: .required_status_checks.contexts, admins: .enforce_admins.enabled}'
gh api "repos/$repository/branches/main/protection" --jq \
    'if .enforce_admins.enabled and .required_status_checks.strict and (.required_status_checks.contexts | index("checks") != null) and .required_pull_request_reviews.required_approving_review_count == 1 and .required_pull_request_reviews.dismiss_stale_reviews and .required_pull_request_reviews.require_last_push_approval and .required_linear_history.enabled and .required_conversation_resolution.enabled and (.allow_force_pushes.enabled | not) and (.allow_deletions.enabled | not) then "Main protection verified." else error("Protection differs from the required policy; inspect GitHub settings.") end'
