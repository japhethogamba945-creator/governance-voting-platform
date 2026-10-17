from django import forms


class StartCountForm(forms.Form):
    reason = forms.CharField(required=False, max_length=1000,
                             widget=forms.Textarea(attrs={"rows": 3}))

    def __init__(self, *args, recount: bool, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["reason"].required = recount
        self.fields["reason"].label = ("Reason for the recount (recorded) *" if recount
                                       else "Note (optional)")


class ContributeForm(forms.Form):
    passphrase = forms.CharField(required=False, strip=False,
                                 widget=forms.PasswordInput(attrs={"autocomplete": "off"}),
                                 label="Your keyholder passphrase")
    backup_code = forms.CharField(required=False, max_length=200,
                                  widget=forms.TextInput(attrs={"autocomplete": "off",
                                                                "spellcheck": "false"}),
                                  label="Or: the sealed backup code",
                                  help_text="Only if a keyholder is unavailable. Leave the "
                                            "passphrase empty.")

    def clean(self):
        data = super().clean()
        if bool(data.get("passphrase")) == bool(data.get("backup_code", "").strip()):
            raise forms.ValidationError("Enter either your passphrase or the backup code.")
        return data


class TieForm(forms.Form):
    elected = forms.MultipleChoiceField(widget=forms.CheckboxSelectMultiple)
    evidence = forms.CharField(max_length=4000, widget=forms.Textarea(attrs={"rows": 4}),
                               label="How the procedure was carried out *",
                               help_text="When and where, who carried it out, who witnessed "
                                         "it, and any reference to minutes or records.")

    def __init__(self, *args, choices, seats: int, **kwargs):
        super().__init__(*args, **kwargs)
        self.seats = seats
        self.fields["elected"].choices = choices
        self.fields["elected"].label = (f"Candidate{'s' if seats != 1 else ''} who take"
                                        f"{'' if seats != 1 else 's'} the {seats} seat"
                                        f"{'s' if seats != 1 else ''} in the tie")

    def clean_elected(self):
        chosen = self.cleaned_data["elected"]
        if len(chosen) != self.seats:
            raise forms.ValidationError(f"Choose exactly {self.seats}.")
        return chosen
