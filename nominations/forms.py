from django import forms

from members.models import Member


class NominateForm(forms.Form):
    WHO = [("self", "Myself"), ("other", "Another member")]

    who = forms.ChoiceField(choices=WHO, widget=forms.RadioSelect, initial="self",
                            label="Who are you nominating?")
    nominee_number = forms.CharField(max_length=32, required=False,
                                     label="Nominee's member number",
                                     help_text="Only if you are nominating another member.")
    proposer_number = forms.CharField(max_length=32, required=False,
                                      label="Proposer's member number",
                                      help_text="For a self-nomination, if a proposer is "
                                                "required. They will be asked to confirm.")
    seconder_number = forms.CharField(max_length=32, required=False,
                                      label="Seconder's member number",
                                      help_text="They will be asked to confirm.")
    statement = forms.CharField(max_length=4000, required=False,
                                widget=forms.Textarea(attrs={"rows": 6}),
                                label="Candidate statement (optional, up to 4,000 characters)")

    @staticmethod
    def _member(number: str, label: str):
        number = (number or "").strip()
        if not number:
            return None
        member = Member.objects.filter(member_number=number, status=Member.Status.ACTIVE).first()
        if member is None:
            raise forms.ValidationError(f"No active member has that {label} number.")
        return member

    def clean(self):
        cleaned = super().clean()
        if self.errors:
            return cleaned
        try:
            cleaned["nominee"] = self._member(cleaned.get("nominee_number"), "nominee's")
            cleaned["proposer"] = self._member(cleaned.get("proposer_number"), "proposer's")
            cleaned["seconder"] = self._member(cleaned.get("seconder_number"), "seconder's")
        except forms.ValidationError as exc:
            self.add_error(None, exc)
            return cleaned
        if cleaned["who"] == "other" and cleaned["nominee"] is None:
            self.add_error("nominee_number", "Enter the member number of the person you "
                                             "are nominating.")
        return cleaned


class StatementForm(forms.Form):
    statement = forms.CharField(max_length=4000, required=False,
                                widget=forms.Textarea(attrs={"rows": 6}))


class DocumentForm(forms.Form):
    file = forms.FileField(label="Supporting document (PDF, JPEG or PNG, at most 5 MB)")


class DecisionForm(forms.Form):
    DECISIONS = [("approve", "Approve"), ("reject", "Reject")]
    decision = forms.ChoiceField(choices=DECISIONS, widget=forms.RadioSelect)
    reason = forms.CharField(max_length=1000, required=False,
                             widget=forms.Textarea(attrs={"rows": 3}),
                             label="Reason (required to reject; recorded in the audit trail)")

    def clean(self):
        cleaned = super().clean()
        if cleaned.get("decision") == "reject" and not (cleaned.get("reason") or "").strip():
            self.add_error("reason", "A reason is required to reject a nomination.")
        return cleaned
