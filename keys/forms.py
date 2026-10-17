from django import forms

from .crypto import MIN_PASSPHRASE_LENGTH


class PassphraseForm(forms.Form):
    current_passphrase = forms.CharField(
        required=False, strip=False, label="Current keyholder passphrase",
        widget=forms.PasswordInput(attrs={"autocomplete": "current-password"}))
    passphrase = forms.CharField(
        strip=False, min_length=MIN_PASSPHRASE_LENGTH, label="New keyholder passphrase",
        help_text=f"At least {MIN_PASSPHRASE_LENGTH} characters; several unrelated words work "
                  "well. This is not your sign-in password, and it cannot be recovered.",
        widget=forms.PasswordInput(attrs={"autocomplete": "new-password"}))
    passphrase_again = forms.CharField(strip=False, label="Repeat the new passphrase",
                                       widget=forms.PasswordInput(
                                           attrs={"autocomplete": "new-password"}))

    def __init__(self, *args, has_key: bool, **kwargs):
        super().__init__(*args, **kwargs)
        if not has_key:
            del self.fields["current_passphrase"]
        else:
            self.fields["current_passphrase"].required = True

    def clean(self):
        cleaned = super().clean()
        if cleaned.get("passphrase") != cleaned.get("passphrase_again"):
            self.add_error("passphrase_again", "The two passphrases do not match.")
        return cleaned
