from datetime import datetime

from django import forms
from django.utils import timezone

from members.importing import categories_for

from . import rules as R
from .models import Election, Meeting

DATETIME_WIDGET = forms.DateTimeInput(attrs={"type": "datetime-local"}, format="%Y-%m-%dT%H:%M")


class MeetingForm(forms.ModelForm):
    class Meta:
        model = Meeting
        fields = ["title", "kind", "scheduled_at", "location", "is_remote"]
        widgets = {"scheduled_at": DATETIME_WIDGET}
        labels = {"is_remote": "Held remotely"}


class ElectionForm(forms.ModelForm):
    # Declared explicitly: the default field would query the tenant-scoped manager
    # at import time, outside any organisation context.
    meeting = forms.ModelChoiceField(queryset=Meeting.all_objects.none(), required=False,
                                     label="Meeting (optional)")

    class Meta:
        model = Election
        fields = ["title", "meeting", "seats", "opens_at", "closes_at"]
        widgets = {"opens_at": DATETIME_WIDGET, "closes_at": DATETIME_WIDGET}
        labels = {"seats": "Number of seats", "opens_at": "Voting opens",
                  "closes_at": "Voting closes"}
        help_texts = {"opens_at": "Nairobi time.", "closes_at": "Nairobi time."}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["meeting"].queryset = Meeting.objects.all()
        self.fields["seats"].widget.attrs.update({"min": 1, "max": 50})

    def clean_seats(self):
        seats = self.cleaned_data["seats"]
        if not 1 <= seats <= 50:
            raise forms.ValidationError("Choose between 1 and 50 seats.")
        return seats

    def clean(self):
        cleaned = super().clean()
        opens, closes = cleaned.get("opens_at"), cleaned.get("closes_at")
        if opens and closes and closes <= opens:
            self.add_error("closes_at", "Voting must close after it opens.")
        if opens and opens <= timezone.now() and "opens_at" in self.changed_data:
            self.add_error("opens_at", "Choose a time in the future.")
        return cleaned


def _choices(mapping):
    return list(mapping.items())


class RulesForm(forms.Form):
    """Edits the rules document. The JSON is built in :meth:`to_rules`."""

    max_selections = forms.IntegerField(label="Maximum candidates a member may choose",
                                        min_value=1)
    min_selections = forms.IntegerField(label="Minimum candidates a member must choose",
                                        min_value=0)
    partial_ballots_allowed = forms.BooleanField(
        required=False, label="Members may choose fewer than the maximum")
    blank_ballot_allowed = forms.BooleanField(required=False,
                                              label="Members may submit a blank ballot")
    categories = forms.MultipleChoiceField(widget=forms.CheckboxSelectMultiple,
                                           label="Eligible membership categories")
    subscription_status = forms.MultipleChoiceField(
        widget=forms.CheckboxSelectMultiple, label="Eligible subscription statuses",
        choices=[(s, s.title() if s != "n/a" else "Not applicable")
                 for s in R.SUBSCRIPTION_STATUSES])
    joined_before = forms.DateTimeField(
        required=False, widget=DATETIME_WIDGET, label="Membership cut-off (optional)",
        help_text="Only members who joined before this time are eligible. Leave empty for "
                  "no cut-off. Confirm any cut-off against your governing documents.")
    self_nomination = forms.BooleanField(required=False, label="Members may nominate themselves")
    proposer_required = forms.BooleanField(required=False, label="A proposer is required")
    seconder_required = forms.BooleanField(required=False, label="A seconder is required")
    documents_required = forms.BooleanField(required=False,
                                            label="A supporting document is required")
    tie_type = forms.ChoiceField(choices=_choices(R.TIE_PROCEDURES), label="Tie-break procedure")
    tie_description = forms.CharField(
        widget=forms.Textarea(attrs={"rows": 3}), label="Tie-break procedure, as approved",
        help_text="Describe exactly how ties will be resolved. This is fixed once the "
                  "election is scheduled.")
    quorum_type = forms.ChoiceField(choices=_choices(R.QUORUM_TYPES), label="Quorum")
    quorum_basis = forms.ChoiceField(choices=_choices(R.QUORUM_BASES),
                                     label="Quorum measured against")
    quorum_threshold = forms.DecimalField(required=False, min_value=0, max_digits=6,
                                          decimal_places=2, label="Quorum threshold")
    early_close_allowed = forms.BooleanField(required=False,
                                             label="The Returning Officer may close voting early")
    candidate_order = forms.ChoiceField(choices=_choices(R.CANDIDATE_ORDERS),
                                        label="Candidate order on the ballot")

    def __init__(self, *args, election, **kwargs):
        self.election = election
        self.categories = categories_for(election.organisation)
        initial = kwargs.pop("initial", None)
        if initial is None:
            source = (election.current_rule_version.rules if election.current_rule_version
                      else R.default_rules(election.seats, self.categories))
            initial = self.initial_from(source)
        super().__init__(*args, initial=initial, **kwargs)
        self.fields["categories"].choices = [(c, c.title()) for c in self.categories]
        self.fields["max_selections"].max_value = election.seats
        self.fields["max_selections"].widget.attrs["max"] = election.seats

    @staticmethod
    def initial_from(rules: dict) -> dict:
        elig, nom = rules["eligibility"], rules["nomination"]
        return {
            "max_selections": rules["max_selections"],
            "min_selections": rules["min_selections"],
            "partial_ballots_allowed": rules["partial_ballots_allowed"],
            "blank_ballot_allowed": rules["blank_ballot_allowed"],
            "categories": elig["categories"],
            "subscription_status": elig["subscription_status"],
            "joined_before": (timezone.localtime(datetime.fromisoformat(elig["joined_before"]))
                              if elig.get("joined_before") else None),
            **{k: nom[k] for k in ("self_nomination", "proposer_required",
                                   "seconder_required", "documents_required")},
            "tie_type": rules["tie_procedure"]["type"],
            "tie_description": rules["tie_procedure"]["description"],
            "quorum_type": rules["quorum"]["type"],
            "quorum_basis": rules["quorum"]["basis"],
            "quorum_threshold": rules["quorum"]["threshold"],
            "early_close_allowed": rules["early_close_allowed"],
            "candidate_order": rules["candidate_order"],
        }

    def to_rules(self) -> dict:
        d = self.cleaned_data
        threshold = d.get("quorum_threshold") or 0
        threshold = int(threshold) if d["quorum_type"] == "absolute" else float(threshold)
        cutoff = d.get("joined_before")
        return {
            "schema": R.SCHEMA,
            "counting_method": "plurality_multi_seat",
            "min_selections": d["min_selections"],
            "max_selections": d["max_selections"],
            "partial_ballots_allowed": d["partial_ballots_allowed"],
            "blank_ballot_allowed": d["blank_ballot_allowed"],
            "eligibility": {
                "member_status": ["active"],
                "categories": sorted(d["categories"]),
                "subscription_status": sorted(d["subscription_status"]),
                "joined_before": cutoff.isoformat() if cutoff else None,
            },
            "nomination": {k: d[k] for k in ("self_nomination", "proposer_required",
                                             "seconder_required", "documents_required")},
            "tie_procedure": {"type": d["tie_type"], "description": d["tie_description"].strip()},
            "quorum": {"type": d["quorum_type"], "basis": d["quorum_basis"],
                       "threshold": threshold},
            "early_close_allowed": d["early_close_allowed"],
            "turnout_visible_to_members": False,
            "candidate_order": d["candidate_order"],
        }

    def clean(self):
        cleaned = super().clean()
        if self.errors:
            return cleaned
        for problem in R.validate_rules(self.to_rules(), self.election.seats, self.categories):
            self.add_error(None, problem)
        return cleaned


class TransitionForm(forms.Form):
    reason = forms.CharField(required=False, max_length=1000,
                             widget=forms.Textarea(attrs={"rows": 3}))

    def __init__(self, *args, reason_required: bool, keyholder_choices=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["reason"].required = reason_required
        self.fields["reason"].label = ("Reason (recorded in the election history) *"
                                       if reason_required else "Note (optional)")
        if keyholder_choices is not None:
            self.fields["keyholders"] = forms.MultipleChoiceField(
                choices=keyholder_choices, widget=forms.CheckboxSelectMultiple,
                label="Choose two other keyholders",
                help_text="You are the first keyholder. Choose two other officials who have "
                          "set a keyholder passphrase. A sealed backup is created as well.")

    def clean_keyholders(self):
        chosen = self.cleaned_data["keyholders"]
        if len(chosen) != 2:
            raise forms.ValidationError("Choose exactly two other keyholders.")
        return chosen
