import re
import traceback
from jira import JIRA, JIRAError

from pr_agent.config_loader import get_settings
from pr_agent.git_providers import GithubProvider
from pr_agent.log import get_logger

# Compile the regex pattern once, outside the function
GITHUB_TICKET_PATTERN = re.compile(
     r'(https://github[^/]+/[^/]+/[^/]+/issues/\d+)|(\b(\w+)/(\w+)#(\d+)\b)|(#\d+)'
)

def find_jira_tickets(text):
    # Regular expression patterns for JIRA tickets
    patterns = [
        r'\b[A-Z0-9]{2,10}-\d{1,7}\b',  # JIRA ticket format (e.g., S4R-1234)
        r'(?:https?://[^\s/]+/browse/)?([A-Z0-9]{2,10}-\d{1,7})\b'  # JIRA URL or just the ticket
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


def extract_ticket_links_from_pr_description(pr_description, repo_path, base_url_html='https://github.com'):
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
                github_tickets.add(f'{base_url_html.strip("/")}/{owner}/{repo}/issues/{issue_number}')
            else:  # #123 format
                issue_number = match[5][1:]  # remove #
                if issue_number.isdigit() and len(issue_number) < 5 and repo_path:
                    github_tickets.add(f'{base_url_html.strip("/")}/{repo_path}/issues/{issue_number}')

        if len(github_tickets) > 3:
            get_logger().info(f"Too many tickets found in PR description: {len(github_tickets)}")
            # Limit the number of tickets to 3
            github_tickets = set(list(github_tickets)[:3])
    except Exception as e:
        get_logger().error(f"Error extracting tickets error= {e}",
                           artifact={"traceback": traceback.format_exc()})

    return list(github_tickets)


async def extract_tickets(git_provider):
    MAX_TICKET_CHARACTERS = 10000
    tickets_content = []

    try:
        user_description = git_provider.get_user_description()

        # Extract GitHub tickets (existing logic)
        if isinstance(git_provider, GithubProvider):
            github_tickets = extract_ticket_links_from_pr_description(user_description, git_provider.repo, git_provider.base_url_html)
            if github_tickets:
                for ticket in github_tickets:
                    repo_name, original_issue_number = git_provider._parse_issue_url(ticket)

                    try:
                        issue_main = git_provider.repo_obj.get_issue(original_issue_number)
                    except Exception as e:
                        get_logger().error(f"Error getting GitHub main issue: {e}",
                                           artifact={"traceback": traceback.format_exc()})
                        continue

                    issue_body_str = issue_main.body or ""
                    if len(issue_body_str) > MAX_TICKET_CHARACTERS:
                        issue_body_str = issue_body_str[:MAX_TICKET_CHARACTERS] + "..."

                    # Extract sub-issues
                    sub_issues_content = []
                    try:
                        sub_issues = git_provider.fetch_sub_issues(ticket)
                        for sub_issue_url in sub_issues:
                            try:
                                sub_repo, sub_issue_number = git_provider._parse_issue_url(sub_issue_url)
                                sub_issue = git_provider.repo_obj.get_issue(sub_issue_number)

                                sub_body = sub_issue.body or ""
                                if len(sub_body) > MAX_TICKET_CHARACTERS:
                                    sub_body = sub_body[:MAX_TICKET_CHARACTERS] + "..."

                                sub_issues_content.append({
                                    'ticket_url': sub_issue_url,
                                    'title': sub_issue.title,
                                    'body': sub_body
                                })
                            except Exception as e:
                                get_logger().warning(f"Failed to fetch GitHub sub-issue content for {sub_issue_url}: {e}")
                    except Exception as e:
                        get_logger().warning(f"Failed to fetch GitHub sub-issues for {ticket}: {e}")

                    # Extract labels
                    labels = []
                    try:
                        for label in issue_main.labels:
                            labels.append(label.name if hasattr(label, 'name') else label)
                    except Exception as e:
                        get_logger().error(f"Error extracting GitHub labels error= {e}",
                                           artifact={"traceback": traceback.format_exc()})

                    tickets_content.append({
                        'ticket_id': f"GH-{issue_main.number}", # Prefix GH for GitHub issues
                        'ticket_url': ticket,
                        'title': issue_main.title,
                        'body': issue_body_str,
                        'labels': ", ".join(labels),
                        'sub_issues': sub_issues_content
                    })

        # Extract Jira tickets
        jira_keys = find_jira_tickets(user_description)
        if jira_keys and get_settings().get("jira.enable_jira_integration", True): # Check if Jira integration enabled
            jira_url = get_settings().get("jira.jira_base_url")
            jira_email = get_settings().get("jira.jira_api_email")
            jira_token = get_settings().get("jira.jira_api_token")

            if jira_url and jira_email and jira_token:
                try:
                    options = {'server': jira_url}
                    jira_client = JIRA(options=options, basic_auth=(jira_email, jira_token))
                    get_logger().info(f"Successfully connected to Jira: {jira_url}")

                    for key in jira_keys:
                        try:
                            issue = jira_client.issue(key)
                            body = getattr(issue.fields, 'description', '') or ""
                            if len(body) > MAX_TICKET_CHARACTERS:
                                body = body[:MAX_TICKET_CHARACTERS] + "..."

                            labels = getattr(issue.fields, 'labels', []) or []

                            # Extract subtasks (if applicable, adjust fields as needed)
                            subtasks_content = []
                            if hasattr(issue.fields, 'subtasks') and issue.fields.subtasks:
                                for subtask in issue.fields.subtasks:
                                    try:
                                        subtask_issue = jira_client.issue(subtask.key)
                                        subtask_body = getattr(subtask_issue.fields, 'description', '') or ""
                                        if len(subtask_body) > MAX_TICKET_CHARACTERS:
                                           subtask_body = subtask_body[:MAX_TICKET_CHARACTERS] + "..."

                                        subtasks_content.append({
                                            'ticket_url': f"{jira_url}/browse/{subtask.key}",
                                            'title': getattr(subtask_issue.fields, 'summary', ''),
                                            'body': subtask_body
                                        })
                                    except JIRAError as e_sub:
                                        get_logger().warning(f"Failed to fetch Jira subtask {subtask.key} for {key}: {e_sub.text}")
                                    except Exception as e_sub_other:
                                         get_logger().warning(f"An unexpected error occurred fetching Jira subtask {subtask.key}: {e_sub_other}")


                            tickets_content.append({
                                'ticket_id': key,
                                'ticket_url': f"{jira_url}/browse/{key}",
                                'title': getattr(issue.fields, 'summary', ''),
                                'body': body,
                                'status': getattr(issue.fields.status, 'name', 'Unknown'),
                                'labels': ", ".join(labels),
                                'sub_issues': subtasks_content # Using 'sub_issues' key for consistency
                            })
                            get_logger().info(f"Successfully fetched Jira ticket: {key}")

                        except JIRAError as e:
                            get_logger().error(f"Error fetching Jira ticket {key}: {e.status_code} - {e.text}",
                                                artifact={"traceback": traceback.format_exc()})
                        except Exception as e:
                             get_logger().error(f"An unexpected error occurred fetching Jira ticket {key}: {e}",
                                                artifact={"traceback": traceback.format_exc()})

                except JIRAError as e:
                    get_logger().error(f"Failed to connect to Jira: {e.status_code} - {e.text}",
                                       artifact={"traceback": traceback.format_exc()})
                except Exception as e:
                     get_logger().error(f"An unexpected error occurred connecting to Jira: {e}",
                                        artifact={"traceback": traceback.format_exc()})
            else:
                get_logger().warning("Jira configuration (URL, email, token) is incomplete. Skipping Jira ticket fetch.")
        elif jira_keys:
             get_logger().info("Jira integration disabled ('jira.enable_jira_integration' is false). Skipping Jira ticket fetch.")


        return tickets_content

    except Exception as e:
        get_logger().error(f"Error extracting tickets (main block): {e}",
                           artifact={"traceback": traceback.format_exc()})
        return [] # Return empty list on error


async def extract_and_cache_pr_tickets(git_provider, vars):
    if not get_settings().get('pr_reviewer.require_ticket_analysis_review', False):
        vars['related_tickets'] = [] # Ensure it's initialized if review is disabled
        return

    related_tickets_cache_key = f"related_tickets_{git_provider.pr_url}"
    cached_data = get_settings().get(related_tickets_cache_key)

    # Check if cache is valid (e.g., based on description hash or timestamp if needed)
    # For now, we just check if it exists
    if cached_data:
         get_logger().info("Using cached tickets", artifact={"tickets": cached_data})
         vars['related_tickets'] = cached_data
         # Optionally update the global settings cache if needed, though maybe better to keep it request-specific
         # get_settings().set(related_tickets_cache_key, cached_data)
         return

    # If no valid cache, extract fresh data
    tickets_content = await extract_tickets(git_provider)

    if tickets_content:
        # The extract_tickets function now returns a flat list including main and sub-issues appropriately formatted
        vars['related_tickets'] = tickets_content
        get_settings().set(related_tickets_cache_key, tickets_content) # Cache the fetched data
        get_logger().info("Extracted and cached tickets from PR description",
                          artifact={"tickets": tickets_content})
    else:
         vars['related_tickets'] = [] # Ensure it's an empty list if no tickets found
         get_logger().info("No relevant tickets found or failed to extract.")
         # Optionally cache the empty result too, to avoid re-fetching if description hasn't changed
         get_settings().set(related_tickets_cache_key, [])


def check_tickets_relevancy():
    # This function might need refinement based on how relevancy is determined
    # For now, it just returns True
    return True
