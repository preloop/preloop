# Deleting your account

To ask for your account to be deleted, open **`/delete-account`** on your
Preloop server (for the hosted service, `https://preloop.ai/delete-account`).
Mobile app store listings link to the same page.

The page sends a request to the team that runs the server. They delete the
account by hand, usually within 30 days, and confirm by email. A request is
passed on only once it has been proven to come from the account's owner.

## While signed in

Open the page in a browser where you are signed in and press **Request
Account Deletion**. The request is made for the account you are signed in to;
the page does not ask which account you mean.

Only an **owner** of the account can ask for it to be deleted. If you are the
only member of a personal account, you can ask too. Other members see a
message asking them to contact an owner.

## While signed out

Enter the email address of your account and press **Request Account
Deletion**. If the address belongs to an account and has been verified, we
send a confirmation link to it. Open the link and press **Confirm deletion
request**. Only then is the request passed on. The link works once and
expires after an hour.

The page shows the same message whether or not the address belongs to an
account, so it cannot be used to check who has one. No link is sent to an
address that was never verified; sign in instead, or verify the address
first.

If your server cannot send email, the page asks you to sign in to make the
request.

## Limits

The reason for leaving is optional and up to 1,000 characters. Requests are
limited per client address and per account or email address (three an hour
by default), so a page reload or a second click does not send duplicates.

## What the team sees

The notification says whether the request came from a **signed-in session**
or was confirmed through the **link emailed to the account's verified
address**. Your username, email address and account id are taken from the
account itself, not from the form. Each confirmed request also writes an
`account_deletion_requested` entry to the account's audit log.
