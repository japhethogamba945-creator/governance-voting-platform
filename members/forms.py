from django import forms
from django.contrib.auth.password_validation import validate_password

from .importing import categories_for
from .models import Member


class MemberForm(forms.ModelForm):
    class Meta:
        model = Member
        fields = ["member_number", "full_name", "email", "phone", "category",
                  "subscription_status", "joined_on"]
        widgets = {"joined_on": forms.DateInput(attrs={"type": "date"})}

    def __init__(self, *args, org, **kwargs):
        super().__init__(*args, **kwargs)
        self.org = org
        self.fields["category"] = forms.ChoiceField(
            choices=[(c, c.title()) for c in categories_for(org)])
        if self.instance.pk and self.instance.user_id:
            self.fields["email"].disabled = True
            self.fields["email"].help_text = "Linked to an account; cannot be changed here."

    def _others(self):
        qs = Member.objects.all()
        return qs.exclude(pk=self.instance.pk) if self.instance.pk else qs

    def clean_member_number(self):
        value = self.cleaned_data["member_number"].strip()
        if self._others().filter(member_number=value).exists():
            raise forms.ValidationError("Another member already has this number.")
        return value

    def clean_email(self):
        value = (self.cleaned_data.get("email") or "").strip().lower()
        if value and self._others().filter(email=value).exists():
            raise forms.ValidationError("Another member already uses this email.")
        return value


class ReasonForm(forms.Form):
    reason = forms.CharField(max_length=500, widget=forms.Textarea(attrs={"rows": 3}))


class ImportUploadForm(forms.Form):
    file = forms.FileField(label="CSV file")


class AcceptInvitationForm(forms.Form):
    password1 = forms.CharField(label="Choose a password", strip=False,
                                widget=forms.PasswordInput(attrs={"autocomplete": "new-password"}))
    password2 = forms.CharField(label="Repeat the password", strip=False,
                                widget=forms.PasswordInput(attrs={"autocomplete": "new-password"}))

    def __init__(self, *args, user, **kwargs):
        super().__init__(*args, **kwargs)
        self.user = user

    def clean(self):
        cleaned = super().clean()
        p1, p2 = cleaned.get("password1"), cleaned.get("password2")
        if p1 and p2 and p1 != p2:
            raise forms.ValidationError("The two passwords do not match.")
        if p1:
            validate_password(p1, user=self.user)
        return cleaned
