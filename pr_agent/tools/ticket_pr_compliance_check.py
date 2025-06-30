import asyncio
import re
import traceback

from jira import JIRA, JIRAError

from pr_agent.config_loader import get_settings
from pr_agent.git_providers import AzureDevopsProvider, GithubProvider
from pr_agent.log import get_logger

# Compile the regex pattern once, outside the function
GITHUB_TICKET_PATTERN = re.compile(r"(https://github[^/]+/[^/]+/[^/]+/issues/\d+)|(\b(\w+)/(\w+)#(\d+)\b)|(#\d+)")
MAX_TICKET_CHARACTERS = 10000


def find_jira_tickets(text):
    # Regular expression patterns for JIRA tickets
    patterns = [
        r"\b[A-Z0-9]{2,10}-\d{1,7}\b",  # JIRA ticket format (e.g., S4R-1234)
        r"(?:https?://[^\s/]+/browse/)?([A-Z0-9]{2,10}-\d{1,7})\b",  # JIRA URL or just the ticket
    ]

    tickets = set()
    for pattern in patterns:
        matches = re.findall(pattern, text)
        for match in matches:
            if isinstance(match, tuple):
                # If it's a tuple (from the URL pattern), take the last non-empty group
                ticket = next((m for m in reversed(match) if m), None)
            else:
                ticket = match
            if ticket:
                tickets.add(ticket)

    return list(tickets)


def extract_ticket_links_from_pr_description(pr_description, repo_path, base_url_html="https://github.com"):
    """
    Extract all ticket links from PR description
    """
    github_tickets = set()
    try:
        # Use the updated pattern to find matches
        matches = GITHUB_TICKET_PATTERN.findall(pr_description)

        for match in matches:
            if match[0]:  # Full URL match
                github_tickets.add(match[0])
            elif match[1]:  # Shorthand notation match: owner/repo#issue_number
                owner, repo, issue_number = match[2], match[3], match[4]
                github_tickets.add(f"{base_url_html.strip('/')}/{owner}/{repo}/issues/{issue_number}")
            else:  # #123 format
                issue_number = match[5][1:]  # remove #
                if issue_number.isdigit() and len(issue_number) < 5 and repo_path:
                    github_tickets.add(f"{base_url_html.strip('/')}/{repo_path}/issues/{issue_number}")

        if len(github_tickets) > 3:
            get_logger().info(f"Too many tickets found in PR description: {len(github_tickets)}")
            # Limit the number of tickets to 3
            github_tickets = set(list(github_tickets)[:3])
    except Exception as e:
        get_logger().error(f"Error extracting tickets error= {e}", artifact={"traceback": traceback.format_exc()})

    return list(github_tickets)


def _truncate_text(text, max_length=MAX_TICKET_CHARACTERS):
    """Helper function to truncate text to a maximum length."""
    if not text:
        return ""
    if len(text) > max_length:
        return text[:max_length] + "..."
    return text


def _extract_github_labels(issue_main):
    """Helper function to extract labels from a GitHub issue."""
    labels = []
    try:
        for label in issue_main.labels:
            labels.append(label.name if hasattr(label, "name") else label)
    except Exception as e:
        get_logger().error(f"Error extracting GitHub labels error= {e}", artifact={"traceback": traceback.format_exc()})
    return labels


def _extract_github_sub_issues(git_provider, ticket):
    """Helper function to extract sub-issues from a GitHub ticket."""
    sub_issues_content = []
    try:
        sub_issues = git_provider.fetch_sub_issues(ticket)
        for sub_issue_url in sub_issues:
            try:
                sub_repo, sub_issue_number = git_provider._parse_issue_url(sub_issue_url)
                sub_issue = git_provider.repo_obj.get_issue(sub_issue_number)

                sub_body = _truncate_text(sub_issue.body)
                sub_issues_content.append({"ticket_url": sub_issue_url, "title": sub_issue.title, "body": sub_body})
            except Exception as e:
                get_logger().warning(f"Failed to fetch GitHub sub-issue content for {sub_issue_url}: {e}")
    except Exception as e:
        get_logger().warning(f"Failed to fetch GitHub sub-issues for {ticket}: {e}")

    return sub_issues_content


def _extract_github_tickets(git_provider, user_description):
    """Extract GitHub tickets and their details."""
    tickets_content = []

    github_tickets = extract_ticket_links_from_pr_description(
        user_description, git_provider.repo, git_provider.base_url_html
    )

    if not github_tickets:
        return tickets_content

    for ticket in github_tickets:
        try:
            repo_name, original_issue_number = git_provider._parse_issue_url(ticket)
            issue_main = git_provider.repo_obj.get_issue(original_issue_number)
        except Exception as e:
            get_logger().error(f"Error getting GitHub main issue: {e}", artifact={"traceback": traceback.format_exc()})
            continue

        issue_body_str = _truncate_text(issue_main.body)
        sub_issues_content = _extract_github_sub_issues(git_provider, ticket)
        labels = _extract_github_labels(issue_main)

        tickets_content.append(
            {
                "ticket_id": f"GH-{issue_main.number}",  # Prefix GH for GitHub issues
                "ticket_url": ticket,
                "title": issue_main.title,
                "body": issue_body_str,
                "labels": ", ".join(labels),
                "sub_issues": sub_issues_content,
            }
        )

    return tickets_content


def _validate_jira_config(jira_url, jira_email, jira_token):
    """Validate Jira configuration and return missing config names if any."""
    missing_configs = []
    if not jira_url or not jira_url.strip():
        missing_configs.append("jira_base_url")
    if not jira_email or not jira_email.strip():
        missing_configs.append("jira_api_email")
    if not jira_token or not jira_token.strip():
        missing_configs.append("jira_api_token")

    return missing_configs


def _extract_jira_subtasks(jira_client, issue, jira_url):
    """Helper function to extract Jira subtasks."""
    subtasks_content = []
    if hasattr(issue.fields, "subtasks") and issue.fields.subtasks:
        for subtask in issue.fields.subtasks:
            try:
                subtask_issue = jira_client.issue(subtask.key)
                subtask_body = _truncate_text(getattr(subtask_issue.fields, "description", ""))

                subtasks_content.append(
                    {
                        "ticket_url": f"{jira_url}/browse/{subtask.key}",
                        "title": getattr(subtask_issue.fields, "summary", ""),
                        "body": subtask_body,
                    }
                )
            except JIRAError as e_sub:
                get_logger().warning(f"Failed to fetch Jira subtask {subtask.key}: {e_sub.text}")
            except Exception as e_sub_other:
                get_logger().warning(f"An unexpected error occurred fetching Jira subtask {subtask.key}: {e_sub_other}")

    return subtasks_content


def _extract_single_jira_ticket(jira_client, key, jira_url):
    """Extract a single Jira ticket and its details."""
    try:
        issue = jira_client.issue(key)
        body = _truncate_text(getattr(issue.fields, "description", ""))
        labels = getattr(issue.fields, "labels", []) or []
        subtasks_content = _extract_jira_subtasks(jira_client, issue, jira_url)

        ticket_data = {
            "ticket_id": key,
            "ticket_url": f"{jira_url}/browse/{key}",
            "title": getattr(issue.fields, "summary", ""),
            "body": body,
            "status": getattr(issue.fields.status, "name", "Unknown"),
            "labels": ", ".join(labels),
            "sub_issues": subtasks_content,
        }

        get_logger().info(f"Successfully fetched Jira ticket: {key}")
        return ticket_data

    except JIRAError as e:
        # Avoid logging potentially sensitive Jira response details
        get_logger().error(
            f"Error fetching Jira ticket {key}: HTTP {e.status_code}",
            artifact={"traceback": traceback.format_exc()},
        )
    except Exception as e:
        get_logger().error(
            f"An unexpected error occurred fetching Jira ticket {key}: {e}",
            artifact={"traceback": traceback.format_exc()},
        )

    return None


def _extract_jira_tickets_sync(user_description):
    """Synchronous helper function to extract Jira tickets and their details."""
    tickets_content = []

    jira_keys = find_jira_tickets(user_description)
    if not jira_keys or not get_settings().get("jira.enable_jira_integration", True):
        if jira_keys:
            get_logger().info(
                "Jira integration disabled ('jira.enable_jira_integration' is false). Skipping Jira ticket fetch."
            )
        return tickets_content

    # Get Jira configuration
    jira_url = get_settings().get("jira.jira_base_url")
    jira_email = get_settings().get("jira.jira_api_email")
    jira_token = get_settings().get("jira.jira_api_token")

    # Validate configuration
    missing_configs = _validate_jira_config(jira_url, jira_email, jira_token)
    if missing_configs:
        get_logger().warning(
            f"Jira configuration is incomplete or contains empty values. "
            f"Missing or invalid: {', '.join(missing_configs)}. Skipping Jira ticket fetch."
        )
        return tickets_content

    # Connect to Jira and extract tickets
    try:
        options = {"server": jira_url}
        # Add timeout to prevent hanging on connection issues
        jira_client = JIRA(options=options, basic_auth=(jira_email, jira_token), timeout=15)
        get_logger().info(f"Successfully connected to Jira: {jira_url}")

        for key in jira_keys:
            ticket_data = _extract_single_jira_ticket(jira_client, key, jira_url)
            if ticket_data:
                tickets_content.append(ticket_data)

    except JIRAError as e:
        # Avoid logging potentially sensitive Jira connection details
        get_logger().error(
            f"Failed to connect to Jira: HTTP {e.status_code}",
            artifact={"traceback": traceback.format_exc()},
        )
    except (ConnectionError, TimeoutError, OSError) as e:
        # Handle network-related errors specifically
        get_logger().error(
            f"Network error connecting to Jira: {type(e).__name__}: {e}",
            artifact={"traceback": traceback.format_exc()},
        )
    except Exception as e:
        get_logger().error(
            f"An unexpected error occurred connecting to Jira: {e}",
            artifact={"traceback": traceback.format_exc()},
        )

    return tickets_content


async def _extract_jira_tickets(user_description):
    """Async wrapper to extract Jira tickets using thread pool to prevent event loop blocking."""
    try:
        # Offload the synchronous Jira operations to a thread pool
        tickets_content = await asyncio.to_thread(_extract_jira_tickets_sync, user_description)
        return tickets_content
    except Exception as e:
        get_logger().error(
            f"Error in async Jira ticket extraction: {e}",
            artifact={"traceback": traceback.format_exc()},
        )
        return []


def _extract_azure_devops_tickets(git_provider):
    """Extract Azure DevOps tickets and their details."""
    tickets_content = []

    try:
        tickets_info = git_provider.get_linked_work_items()
        for ticket in tickets_info:
            try:
                ticket_body_str = _truncate_text(ticket.get("body", ""))

                tickets_content.append(
                    {
                        "ticket_id": ticket.get("id"),
                        "ticket_url": ticket.get("url"),
                        "title": ticket.get("title"),
                        "body": ticket_body_str,
                        "requirements": ticket.get("acceptance_criteria", ""),
                        "labels": ", ".join(ticket.get("labels", [])),
                    }
                )
            except Exception as e:
                get_logger().error(
                    f"Error processing Azure DevOps ticket: {e}",
                    artifact={"traceback": traceback.format_exc()},
                )
    except Exception as e:
        get_logger().error(
            f"Error extracting Azure DevOps tickets: {e}",
            artifact={"traceback": traceback.format_exc()},
        )

    return tickets_content


async def extract_tickets(git_provider):
    """
    Extract tickets from various sources (GitHub, Jira, Azure DevOps) based on the git provider.

    This function coordinates the extraction from different ticket sources and returns
    a unified list of ticket information.
    """
    tickets_content = []

    try:
        user_description = git_provider.get_user_description()

        # Extract tickets based on provider type and available information
        if isinstance(git_provider, GithubProvider):
            tickets_content.extend(_extract_github_tickets(git_provider, user_description))
        elif isinstance(git_provider, AzureDevopsProvider):
            tickets_content.extend(_extract_azure_devops_tickets(git_provider))

        # Always try to extract Jira tickets from description (regardless of git provider)
        jira_tickets = await _extract_jira_tickets(user_description)
        tickets_content.extend(jira_tickets)

    except Exception as e:
        get_logger().error(
            f"Error extracting tickets (main block): {e}", artifact={"traceback": traceback.format_exc()}
        )

    return tickets_content


async def extract_and_cache_pr_tickets(git_provider, vars):
    """
    Extract and cache PR tickets with intelligent caching strategy.

    This function handles the caching logic and delegates the actual extraction
    to the extract_tickets function.
    """
    if not get_settings().get("pr_reviewer.require_ticket_analysis_review", False):
        vars["related_tickets"] = []  # Ensure it's initialized if review is disabled
        return

    related_tickets_cache_key = f"related_tickets_{git_provider.pr_url}"
    cached_data = get_settings().get(related_tickets_cache_key)

    # Check if cache is valid (e.g., based on description hash or timestamp if needed)
    # For now, we just check if it exists
    if cached_data:
        get_logger().info("Using cached tickets", artifact={"tickets": cached_data})
        vars["related_tickets"] = cached_data
        return

    # If no valid cache, extract fresh data
    tickets_content = await extract_tickets(git_provider)

    if tickets_content:
        # Cache the fetched data and update vars
        vars["related_tickets"] = tickets_content
        get_settings().set(related_tickets_cache_key, tickets_content)
        get_logger().info("Extracted and cached tickets from PR description", artifact={"tickets": tickets_content})
    else:
        vars["related_tickets"] = []  # Ensure it's an empty list if no tickets found
        get_logger().info("No relevant tickets found or failed to extract.")
        # Optionally cache the empty result too, to avoid re-fetching if description hasn't changed
        get_settings().set(related_tickets_cache_key, [])


def check_tickets_relevancy():
    """
    Check if tickets are relevant to the current context.

    This function might need refinement based on how relevancy is determined.
    For now, it just returns True.
    """
    return True
