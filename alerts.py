"""Email alerts over SMTP. Without SMTP settings it only prints, so local runs need no email setup."""
import smtplib
from email.message import EmailMessage


class EmailAlerter:
    def __init__(self, env):
        self.host = env.get("SMTP_HOST")
        self.port = int(env.get("SMTP_PORT") or 587)
        self.user = env.get("SMTP_USER")
        self.password = env.get("SMTP_PASSWORD")
        self.to = env.get("ALERT_TO")
        self.sender = env.get("ALERT_FROM") or self.user

    @property
    def enabled(self):
        return bool(self.host and self.to and self.sender)

    def send(self, subject, body):
        """Print the alert, then email it if configured. Return True if the email was sent."""
        print(f"\n[ALERT] {subject}\n{body}")
        if not self.enabled:
            print("(email not configured: printed only)")
            return False
        msg = EmailMessage()
        msg["Subject"], msg["From"], msg["To"] = subject, self.sender, self.to
        msg.set_content(body)
        try:
            with smtplib.SMTP(self.host, self.port, timeout=20) as s:
                s.starttls()
                if self.user:
                    s.login(self.user, self.password or "")
                s.send_message(msg)
            print(f"(emailed {self.to})")
            return True
        except Exception as e:
            print(f"(email failed: {type(e).__name__}: {e})")
            return False
