from typing import Any
import asyncio
from datetime import datetime, timedelta
import email
import imaplib
import smtplib
import logging
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.header import decode_header, make_header
import os
from dotenv import load_dotenv
from mcp.server.models import InitializationOptions
import mcp.types as types
from mcp.server import NotificationOptions, Server
import mcp.server.stdio

# Configure logging
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s - %(levelname)s - %(message)s',
    filename='email_client.log'
)

# Load environment variables from .env file
load_dotenv()

# Email configuration
EMAIL_CONFIG = {
    "email": os.getenv("EMAIL_ADDRESS", "your.email@gmail.com"),
    "password": os.getenv("EMAIL_PASSWORD", "your-app-specific-password"),
    "imap_server": os.getenv("IMAP_SERVER", "imap.gmail.com"),
    "smtp_server": os.getenv("SMTP_SERVER", "smtp.gmail.com"),
    "smtp_port": int(os.getenv("SMTP_PORT", "587"))
}

# Constants
SEARCH_TIMEOUT = 60  # seconds
MAX_EMAILS = 100

server = Server("email")

def _decode_header_value(value: str | None, default: str = "Unknown") -> str:
    """Decode an RFC 2047 encoded header (e.g. '=?UTF-8?B?...?=') into readable text."""
    if not value:
        return default
    try:
        return str(make_header(decode_header(value)))
    except Exception:
        return value

def _decode_payload(part) -> str:
    """Decode a message part's payload using its declared charset, with a safe fallback.

    Falls back to UTF-8 with replacement so emails in other encodings (or with a
    missing/mislabeled charset) stay readable instead of raising UnicodeDecodeError.
    """
    payload = part.get_payload(decode=True)
    if payload is None:
        return ""
    charset = part.get_content_charset() or "utf-8"
    try:
        return payload.decode(charset, errors="replace")
    except (LookupError, TypeError):
        # Unknown charset name — fall back to UTF-8 with replacement
        return payload.decode("utf-8", errors="replace")

def format_email_summary(msg_data: tuple) -> dict:
    """Format an email message into a summary dict with basic information."""
    email_body = email.message_from_bytes(msg_data[0][1])

    return {
        "id": msg_data[0][0].split()[0].decode(),  # Get the email ID
        "from": _decode_header_value(email_body.get("From")),
        "date": email_body.get("Date", "Unknown"),
        "subject": _decode_header_value(email_body.get("Subject"), "No Subject"),
    }

def format_email_content(msg_data: tuple) -> dict:
    """Format an email message into a dict with full content."""
    email_body = email.message_from_bytes(msg_data[0][1])

    # Extract body content
    body = ""
    if email_body.is_multipart():
        # Handle multipart messages
        for part in email_body.walk():
            if part.get_content_type() == "text/plain":
                body = _decode_payload(part)
                break
            elif part.get_content_type() == "text/html":
                # If no plain text found, use HTML content
                if not body:
                    body = _decode_payload(part)
    else:
        # Handle non-multipart messages
        body = _decode_payload(email_body)

    return {
        "from": _decode_header_value(email_body.get("From")),
        "to": _decode_header_value(email_body.get("To")),
        "date": email_body.get("Date", "Unknown"),
        "subject": _decode_header_value(email_body.get("Subject"), "No Subject"),
        "content": body
    }

async def search_emails_async(mail: imaplib.IMAP4_SSL, search_criteria: str) -> list[dict]:
    """Asynchronously search emails with timeout."""
    loop = asyncio.get_event_loop()
    try:
        _, messages = await loop.run_in_executor(None, lambda: mail.search(None, search_criteria))
        if not messages[0]:
            return []
            
        email_list = []
        for num in messages[0].split()[:MAX_EMAILS]:  # Limit to MAX_EMAILS
            _, msg_data = await loop.run_in_executor(None, lambda: mail.fetch(num, '(RFC822)'))
            email_list.append(format_email_summary(msg_data))
            
        return email_list
    except Exception as e:
        raise Exception(f"Error searching emails: {str(e)}")

async def get_email_content_async(mail: imaplib.IMAP4_SSL, email_id: str) -> dict:
    """Asynchronously get full content of a specific email."""
    loop = asyncio.get_event_loop()
    try:
        _, msg_data = await loop.run_in_executor(None, lambda: mail.fetch(email_id, '(RFC822)'))
        return format_email_content(msg_data)
    except Exception as e:
        raise Exception(f"Error fetching email content: {str(e)}")

async def count_emails_async(mail: imaplib.IMAP4_SSL, search_criteria: str) -> int:
    """Asynchronously count emails matching the search criteria."""
    loop = asyncio.get_event_loop()
    try:
        _, messages = await loop.run_in_executor(None, lambda: mail.search(None, search_criteria))
        return len(messages[0].split()) if messages[0] else 0
    except Exception as e:
        raise Exception(f"Error counting emails: {str(e)}")

async def send_email_async(
    to_addresses: list[str],
    subject: str,
    content: str,
    cc_addresses: list[str] | None = None
) -> None:
    """Asynchronously send an email."""
    try:
        # Create message
        msg = MIMEMultipart()
        msg['From'] = EMAIL_CONFIG["email"]
        msg['To'] = ', '.join(to_addresses)
        if cc_addresses:
            msg['Cc'] = ', '.join(cc_addresses)
        msg['Subject'] = subject
        
        # Add body
        msg.attach(MIMEText(content, 'plain', 'utf-8'))
        
        # Connect to SMTP server and send email
        def send_sync():
            with smtplib.SMTP(EMAIL_CONFIG["smtp_server"], EMAIL_CONFIG["smtp_port"]) as server:
                server.set_debuglevel(1)  # Enable debug output
                logging.debug(f"Connecting to {EMAIL_CONFIG['smtp_server']}:{EMAIL_CONFIG['smtp_port']}")
                
                # Start TLS
                logging.debug("Starting TLS")
                server.starttls()
                
                # Login
                logging.debug(f"Logging in as {EMAIL_CONFIG['email']}")
                server.login(EMAIL_CONFIG["email"], EMAIL_CONFIG["password"])
                
                # Send email
                all_recipients = to_addresses + (cc_addresses or [])
                logging.debug(f"Sending email to: {all_recipients}")
                result = server.send_message(msg, EMAIL_CONFIG["email"], all_recipients)
                
                if result:
                    # send_message returns a dict of failed recipients
                    raise Exception(f"Failed to send to some recipients: {result}")
                
                logging.debug("Email sent successfully")
        
        # Run the synchronous send function in the executor
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, send_sync)
        
    except Exception as e:
        logging.error(f"Error in send_email_async: {str(e)}")
        raise

@server.list_tools()
async def handle_list_tools() -> list[types.Tool]:
    """
    List available tools.
    Each tool specifies its arguments using JSON Schema validation.
    """
    return [
        types.Tool(
            name="search-emails",
            description="Search emails within a date range and/or with specific keywords",
            inputSchema={
                "type": "object",
                "properties": {
                    "start_date": {
                        "type": "string",
                        "description": "Start date in YYYY-MM-DD format (optional)",
                    },
                    "end_date": {
                        "type": "string",
                        "description": "End date in YYYY-MM-DD format (optional)",
                    },
                    "keyword": {
                        "type": "string",
                        "description": "Keyword to search in email subject and body (optional)",
                    },
                    "folder": {
                        "type": "string",
                        "description": "Folder to search in ('inbox' or 'sent', defaults to 'inbox')",
                        "enum": ["inbox", "sent"],
                    },
                },
            },
        ),
        types.Tool(
            name="get-email-content",
            description="Get the full content of a specific email by its ID",
            inputSchema={
                "type": "object",
                "properties": {
                    "email_id": {
                        "type": "string",
                        "description": "The ID of the email to retrieve",
                    },
                },
                "required": ["email_id"],
            },
        ),
        types.Tool(
            name="count-daily-emails",
            description="Count emails received for each day in a date range",
            inputSchema={
                "type": "object",
                "properties": {
                    "start_date": {
                        "type": "string",
                        "description": "Start date in YYYY-MM-DD format",
                    },
                    "end_date": {
                        "type": "string",
                        "description": "End date in YYYY-MM-DD format",
                    },
                },
                "required": ["start_date", "end_date"],
            },
        ),
        types.Tool(
            name="send-email",
            description="CONFIRMATION STEP: Actually send the email after user confirms the details. Before calling this, first show the email details to the user for confirmation. Required fields: recipients (to), subject, and content. Optional: CC recipients.",
            inputSchema={
                "type": "object",
                "properties": {
                    "to": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "List of recipient email addresses (confirmed)",
                    },
                    "subject": {
                        "type": "string",
                        "description": "Confirmed email subject",
                    },
                    "content": {
                        "type": "string",
                        "description": "Confirmed email content",
                    },
                    "cc": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "List of CC recipient email addresses (optional, confirmed)",
                    },
                },
                "required": ["to", "subject", "content"],
            },
        ),
    ]

@server.call_tool()
async def handle_call_tool(
    name: str, arguments: dict | None
) -> list[types.TextContent | types.ImageContent | types.EmbeddedResource]:
    """
    Handle tool execution requests.
    Tools can search emails and return results.
    """
    if not arguments:
        arguments = {}
    
    mail = None  # Initialize mail to None for cleanup in the finally block
    try:
        if name == "send-email":
            # Extract email details from arguments
            to_addresses = arguments.get("to", [])
            subject = arguments.get("subject", "")
            content = arguments.get("content", "")
            cc_addresses = arguments.get("cc", [])
            
            # Validate recipient addresses
            if not to_addresses:
                return [types.TextContent(
                    type="text",
                    text="At least one recipient email address is required."
                )]
            
            try:
                # Log email sending attempt
                logging.info("Attempting to send email")
                logging.info(f"To: {to_addresses}")
                logging.info(f"Subject: {subject}")
                logging.info(f"CC: {cc_addresses}")
                
                # Send email asynchronously with a timeout
                async with asyncio.timeout(SEARCH_TIMEOUT):
                    await send_email_async(to_addresses, subject, content, cc_addresses)
                    return [types.TextContent(
                        type="text",
                        text="Email sent successfully! Check email_client.log for detailed logs."
                    )]
            except asyncio.TimeoutError:
                # Handle timeout during email sending
                logging.error("Operation timed out while sending email")
                return [types.TextContent(
                    type="text",
                    text="Operation timed out while sending email."
                )]
            except Exception as e:
                # Log and return error message for email sending failure
                error_msg = str(e)
                logging.error(f"Failed to send email: {error_msg}")
                return [types.TextContent(
                    type="text",
                    text=f"Failed to send email: {error_msg}\n\nPlease check:\n1. Email and password are correct in .env\n2. SMTP settings are correct\n3. Less secure app access is enabled (for Gmail)\n4. Using App Password if 2FA is enabled"
                )]
        
        # Connect to the IMAP server for email operations
        logging.debug(f"Connecting to IMAP server: {EMAIL_CONFIG['imap_server']}")
        mail = imaplib.IMAP4_SSL(EMAIL_CONFIG["imap_server"])
        logging.debug(f"Logging into IMAP server with email: {EMAIL_CONFIG['email']}")
        mail.login(EMAIL_CONFIG["email"], EMAIL_CONFIG["password"])
        logging.debug("IMAP login successful.")
        
        if name == "search-emails":
            # Determine the folder to search
            folder_to_search = arguments.get("folder", "inbox")
            target_imap_folder = ""
            if folder_to_search == "sent":
                target_imap_folder = '"[Gmail]/Sent Mail"'  # For Gmail
                logging.debug(f"Selecting folder: {target_imap_folder}")
                mail.select(target_imap_folder)
            else:
                target_imap_folder = "inbox"
                logging.debug(f"Selecting folder: {target_imap_folder}")
                mail.select(target_imap_folder)
            
            # Retrieve search criteria from arguments
            start_date_str = arguments.get("start_date")
            end_date_str = arguments.get("end_date")
            keyword = arguments.get("keyword")
            
            # Set default dates if not provided
            if not start_date_str:
                start_date_dt = datetime.now() - timedelta(days=7)
            else:
                start_date_dt = datetime.strptime(start_date_str, "%Y-%m-%d")
            
            if not end_date_str:
                end_date_dt = datetime.now()
            else:
                end_date_dt = datetime.strptime(end_date_str, "%Y-%m-%d")

            # Format dates for IMAP search (e.g., "01-Jan-2023")
            imap_start_date = start_date_dt.strftime("%d-%b-%Y")
            imap_end_date = end_date_dt.strftime("%d-%b-%Y")

            # Build search criteria based on date range
            search_criteria_parts = []
            if start_date_dt.date() == end_date_dt.date():
                search_criteria_parts.append(f'ON "{imap_start_date}"')
            else:
                # Include the end date by searching until the day after
                imap_next_day_after_end = (end_date_dt + timedelta(days=1)).strftime("%d-%b-%Y")
                search_criteria_parts.append(f'SINCE "{imap_start_date}"')
                search_criteria_parts.append(f'BEFORE "{imap_next_day_after_end}"')
                
            # Combine keyword criteria with date criteria if provided
            if keyword:
                keyword_criteria = f'(OR SUBJECT "{keyword}" BODY "{keyword}")'
                if len(search_criteria_parts) > 0:
                     search_criteria = f"({keyword_criteria} {' '.join(search_criteria_parts)})"
                else:
                    search_criteria = keyword_criteria  # Should not happen with current date logic
            else:
                search_criteria = ' '.join(search_criteria_parts)
            
            logging.debug(f"Executing IMAP search in folder '{target_imap_folder}' with criteria: {search_criteria}")
            
            try:
                # Perform the email search asynchronously
                async with asyncio.timeout(SEARCH_TIMEOUT):
                    email_list = await search_emails_async(mail, search_criteria)
                    
                # Handle case where no emails are found
                if not email_list:
                    return [types.TextContent(
                        type="text",
                        text="No emails found matching the criteria."
                    )]
                
                # Format the results for display
                result_text = "Found emails:\n\n"
                result_text += "ID | From | Date | Subject\n"
                result_text += "-" * 80 + "\n"
                
                for email_item in email_list:  # Iterate through found emails
                    result_text += f"{email_item['id']} | {email_item['from']} | {email_item['date']} | {email_item['subject']}\n"
                
                result_text += "\nUse get-email-content with an email ID to view the full content of a specific email."
                
                return [types.TextContent(
                    type="text",
                    text=result_text
                )]
                
            except asyncio.TimeoutError:
                # Handle timeout during email search
                logging.error("Search operation timed out.")
                return [types.TextContent(
                    type="text",
                    text="Search operation timed out. Please try with a more specific search criteria."
                )]
            except Exception as e:
                # Log and return error message for search failure
                logging.error(f"Error during email search: {str(e)}")
                return [types.TextContent(type="text", text=f"Error during email search: {str(e)}")]

        elif name == "get-email-content":
            # Retrieve the email ID from arguments
            email_id = arguments.get("email_id")
            if not email_id:
                return [types.TextContent(
                    type="text",
                    text="Email ID is required."
                )]
            
            target_folder_for_get = "inbox"  # Default to INBOX
            try:
                # Select the folder for retrieving email content
                logging.debug(f"Selecting folder '{target_folder_for_get}' for get-email-content.")
                status, select_data = mail.select(target_folder_for_get)
                if status != 'OK':
                    error_message = f"Failed to select folder '{target_folder_for_get}': {select_data[0].decode() if select_data and select_data[0] else 'Unknown error'}"
                    logging.error(error_message)
                    return [types.TextContent(type="text", text=error_message)]
                logging.debug(f"Folder '{target_folder_for_get}' selected successfully for get-email-content.")
            except Exception as e:
                # Log and return error message for folder selection failure
                logging.error(f"Exception selecting folder for get-email-content: {str(e)}")
                return [types.TextContent(type="text", text=f"Error selecting mailbox: {str(e)}")]
            
            try:
                # Fetch the email content asynchronously
                async with asyncio.timeout(SEARCH_TIMEOUT):
                    email_content = await get_email_content_async(mail, email_id)
                    
                # Format the email content for display
                result_text = (
                    f"From: {email_content['from']}\n"
                    f"To: {email_content['to']}\n"
                    f"Date: {email_content['date']}\n"
                    f"Subject: {email_content['subject']}\n"
                    f"\nContent:\n{email_content['content']}"
                )
                
                return [types.TextContent(
                    type="text",
                    text=result_text
                )]
                
            except asyncio.TimeoutError:
                # Handle timeout during email content retrieval
                logging.error(f"Operation timed out while fetching email content for ID {email_id}.")
                return [types.TextContent(
                    type="text",
                    text="Operation timed out while fetching email content."
                )]
            except Exception as e:
                # Log and return error message for content retrieval failure
                logging.error(f"Error fetching email content for ID {email_id}: {str(e)}")
                return [types.TextContent(type="text", text=f"Error fetching email content: {str(e)}")]

        elif name == "count-daily-emails":
            target_folder_for_count = "inbox"  
            try:
                # Select the folder for counting daily emails
                logging.debug(f"Selecting folder '{target_folder_for_count}' for count-daily-emails.")
                status, select_data = mail.select(target_folder_for_count)
                if status != 'OK':
                    error_message = f"Failed to select folder '{target_folder_for_count}': {select_data[0].decode() if select_data and select_data[0] else 'Unknown error'}"
                    logging.error(error_message)
                    return [types.TextContent(type="text", text=error_message)]
                logging.debug(f"Folder '{target_folder_for_count}' selected successfully for count-daily-emails.")
            except Exception as e:
                # Log and return error message for folder selection failure
                logging.error(f"Exception selecting folder for count-daily-emails: {str(e)}")
                return [types.TextContent(type="text", text=f"Error selecting mailbox: {str(e)}")]

            # Retrieve date range for counting emails
            start_date_str = arguments.get("start_date") 
            end_date_str = arguments.get("end_date")    

            # Validate that both start and end dates are provided
            if not start_date_str or not end_date_str:
                return [types.TextContent(type="text", text="Start date and end date are required for count-daily-emails.")]

            # Parse the start and end dates
            start_date = datetime.strptime(start_date_str, "%Y-%m-%d")
            end_date = datetime.strptime(end_date_str, "%Y-%m-%d")
            
            # Prepare result text for daily email counts
            result_text = "Daily email counts:\n\n"
            result_text += "Date | Count\n"
            result_text += "-" * 30 + "\n"
            
            current_date = start_date
            while current_date <= end_date:
                # Format date for IMAP search
                date_str_imap = current_date.strftime("%d-%b-%Y")
                search_criteria = f'(ON "{date_str_imap}")'
                logging.debug(f"Counting emails for date '{date_str_imap}' with criteria: {search_criteria}")
                
                try:
                    # Count emails asynchronously for the current date
                    async with asyncio.timeout(SEARCH_TIMEOUT):
                        count = await count_emails_async(mail, search_criteria) 
                        result_text += f"{current_date.strftime('%Y-%m-%d')} | {count}\n"
                except asyncio.TimeoutError:
                    # Handle timeout during email counting
                    logging.warning(f"Timeout counting emails for date '{date_str_imap}'.")
                    result_text += f"{current_date.strftime('%Y-%m-%d')} | Timeout\n"
                except Exception as e:
                    # Log and return error message for counting failure
                    logging.error(f"Error counting emails for date '{date_str_imap}': {str(e)}")
                    result_text += f"{current_date.strftime('%Y-%m-%d')} | Error\n"

                # Move to the next date
                current_date += timedelta(days=1)
            
            return [types.TextContent(
                type="text",
                text=result_text
            )]
                
        else:
            # Log and raise an error for unknown tool requests
            logging.error(f"Unknown tool called: {name}")
            raise ValueError(f"Unknown tool: {name}")
            
    except Exception as e:
        # Log and return error message for unexpected exceptions
        logging.error(f"An unexpected error occurred in handle_call_tool for tool '{name}': {str(e)}", exc_info=True)
        return [types.TextContent(
            type="text",
            text=f"An unexpected error occurred: {str(e)}"
        )]
    finally:
        if mail:
            try:
                # Close and log out of the IMAP connection
                logging.debug("Closing and logging out IMAP connection.")
                mail.close()
                mail.logout()
                logging.debug("IMAP connection closed and logged out.")
            except Exception as e:
                # Log any errors that occur during close/logout
                logging.warning(f"Error during IMAP close/logout: {str(e)}")
                pass 

async def main():
    # Run the server using stdin/stdout streams
    async with mcp.server.stdio.stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            InitializationOptions(
                server_name="email",
                server_version="0.1.0",
                capabilities=server.get_capabilities(
                    notification_options=NotificationOptions(),
                    experimental_capabilities={},
                ),
            ),
        )

if __name__ == "__main__":
    asyncio.run(main())

