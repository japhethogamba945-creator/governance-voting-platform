from django import forms


class LoginForm(forms.Form):
    email = forms.EmailField(max_length=254, widget=forms.EmailInput(attrs={
        "autocomplete": "username", "autofocus": True}))
    password = forms.CharField(max_length=1024, strip=False, widget=forms.PasswordInput(attrs={
        "autocomplete": "current-password"}))


class TokenForm(forms.Form):
    token = forms.CharField(
        label="Code",
        max_length=16,
        widget=forms.TextInput(attrs={"autocomplete": "one-time-code", "inputmode": "text",
                                      "autofocus": True}),
    )

    def clean_token(self):
        return "".join(self.cleaned_data["token"].split())
