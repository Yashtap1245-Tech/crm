from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from django import forms
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from .models import Account, Contact, Deal, Lead, Organization, Task, Workspace


class StyledForm(forms.Form):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        style(self)


def style(form):
    for field in form.fields.values():
        field.widget.attrs["class"] = (
            "form-check-input"
            if isinstance(field.widget, forms.CheckboxInput)
            else "form-select"
            if isinstance(field.widget, forms.Select)
            else "form-control"
        )
        if isinstance(field, forms.DateField):
            field.widget = forms.DateInput(attrs={"type": "date", "class": "form-control"})


class SignupForm(StyledForm):
    name = forms.CharField(max_length=100, label="Your name")
    email = forms.EmailField()
    password = forms.CharField(widget=forms.PasswordInput, min_length=12)
    confirm_password = forms.CharField(widget=forms.PasswordInput)

    def clean_email(self):
        email = self.cleaned_data["email"].strip().lower()
        if Account.objects.filter(email__iexact=email).exists():
            raise ValidationError("Unable to use this address. Try signing in or resetting your password.")
        return email

    def clean(self):
        data = super().clean()
        if data.get("password") != data.get("confirm_password"):
            self.add_error("confirm_password", "Passwords do not match.")
        if data.get("password"):
            validate_password(
                data["password"], Account(email=data.get("email", ""), first_name=data.get("name", ""))
            )
        return data


class CompanySignupForm(SignupForm):
    company = forms.CharField(max_length=120, label="Company name")


class LoginForm(StyledForm):
    email = forms.EmailField()
    password = forms.CharField(widget=forms.PasswordInput)


class CodeForm(StyledForm):
    code = forms.RegexField(
        r"^\d{6}$",
        label="Six-digit authenticator code",
        widget=forms.TextInput(attrs={"autocomplete": "one-time-code", "inputmode": "numeric"}),
    )


class InviteForm(StyledForm):
    email = forms.EmailField()
    role = forms.ChoiceField(choices=[("member", "Member"), ("admin", "Admin")])


class WorkspaceForm(forms.ModelForm):
    class Meta:
        model = Workspace
        fields = ["name", "timezone", "currency"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        style(self)
        if Deal.objects.exists():
            self.fields["currency"].disabled = True

    def clean_timezone(self):
        value = self.cleaned_data["timezone"]
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValidationError("Use an IANA timezone, for example America/Los_Angeles.")
        return value

    def clean_currency(self):
        value = self.cleaned_data["currency"].upper()
        if value not in ("USD", "EUR", "GBP", "INR", "CAD", "AUD", "JPY", "SGD", "AED", "NZD", "CHF"):
            raise ValidationError(
                "Choose a supported currency: USD, EUR, GBP, INR, CAD, AUD, JPY, SGD, AED, NZD, CHF."
            )
        return value


class RecordForm(forms.ModelForm):
    version = forms.IntegerField(widget=forms.HiddenInput, required=False)

    def __init__(self, *args, workspace, user, **kwargs):
        super().__init__(*args, **kwargs)
        self.instance.workspace = workspace
        for name in ("assigned_to",):
            if name in self.fields:
                self.fields[name].queryset = Account.objects.filter(workspace=workspace, is_active=True)
        for name in ("organization", "contact"):
            if name in self.fields:
                self.fields[name].queryset = self.fields[name].queryset.filter(archived=False)
        if self.instance._state.adding and "assigned_to" in self.fields:
            self.initial["assigned_to"] = user.pk
        self.initial["version"] = self.instance.version if hasattr(self.instance, "version") else 1
        style(self)


class OrganizationForm(RecordForm):
    class Meta:
        model = Organization
        fields = ["name", "website", "industry", "address", "assigned_to"]


class ContactForm(RecordForm):
    class Meta:
        model = Contact
        fields = ["name", "email", "phone", "organization", "source", "assigned_to"]


class LeadForm(RecordForm):
    class Meta:
        model = Lead
        fields = [
            "name",
            "email",
            "phone",
            "source",
            "status",
            "contact",
            "assigned_to",
            "follow_up",
            "qualification",
        ]


class DealForm(RecordForm):
    class Meta:
        model = Deal
        fields = ["name", "contact", "stage", "amount", "expected_close", "loss_reason", "assigned_to"]

    def clean(self):
        data = super().clean()
        if data.get("stage") == "lost" and not data.get("loss_reason"):
            self.add_error("loss_reason", "Please record a loss reason.")
        if data.get("amount") is not None and data["amount"] < 0:
            self.add_error("amount", "Use a nonnegative amount.")
        return data


class TaskForm(forms.ModelForm):
    class Meta:
        model = Task
        fields = ["name", "assigned_to", "due_date"]

    def __init__(self, *args, workspace, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["assigned_to"].queryset = Account.objects.filter(workspace=workspace, is_active=True)
        style(self)


class ImportForm(StyledForm):
    file = forms.FileField(label="CSV file (maximum 500 rows, 1 MB)")


RECORDS = {
    "organizations": (Organization, OrganizationForm, "Organizations"),
    "contacts": (Contact, ContactForm, "Contacts"),
    "leads": (Lead, LeadForm, "Leads"),
    "deals": (Deal, DealForm, "Deals"),
}
