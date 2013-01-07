from django.core.exceptions import PermissionDenied
from django.db.models import Q
from django.http import Http404
from django.shortcuts import redirect
from django.views.generic import CreateView, ListView, DetailView, TemplateView

from ASHMC.main.models import ASHMCRole, DormPresident, GradYear, Semester, Student
from ASHMC.roster.models import Dorm, UserRoom
from .forms import BallotForm, CreateMeasureForm, CreateRestrictionsForm
from .models import (
    Ballot,
    Candidate,
    CandidateUser,
    PersonCandidate,
    Measure,
    Vote,
    PopularityVote,
    PreferentialVote
)

import datetime
import logging
import pytz
import re


logger = logging.getLogger(__name__)


class GenBallotForm(TemplateView):
    template_name = "vote/ballot_create_form_fields.html"

    def get_context_data(self, **kwargs):
        context = super(GenBallotForm, self).get_context_data(**kwargs)
        context['bid'] = kwargs['num']
        context['ballot_types'] = Ballot.TYPES
        return context


class GenCandidateForm(TemplateView):
    template_name = "vote/candidate_create_form_fields.html"

    def get_context_data(self, **kwargs):
        context = super(GenCandidateForm, self).get_context_data(**kwargs)
        context['bid'] = kwargs['bnum']
        context['cid'] = kwargs['cnum']
        return context


class CreateMeasure(CreateView):
    model = Measure
    form_class = CreateMeasureForm

    def get(self, *args, **kwargs):
        # Only the upper eschelons of the council can create measures.
        # President, VP, Dorm President, and Class Presidents only.
        # And super users, obviously.
        if self.request.user.highest_ashmc_role <= ASHMCRole.objects.get(title=ASHMCRole.COUNCIL_ROLES[3]):
            if "Class President" not in self.request.user.highest_ashmc_role.title:
                raise Http404

        return super(CreateMeasure, self).get(*args, **kwargs)

    def get_context_data(self, **kwargs):
        context = super(CreateMeasure, self).get_context_data(**kwargs)

        context['ballot_types'] = Ballot.TYPES
        context['restrictionsform'] = CreateRestrictionsForm()
        return context

    def post(self, *args, **kwargs):
        print self.request.POST

        form_class = self.get_form_class()
        # This setting to None is a hack to make the get_form call work.
        self.object = None
        form = self.get_form(form_class)

        if not form.is_valid():
            return self.form_invalid(form)
        print form.cleaned_data

        # The measure itself is valid; create it -- but DON'T SAVE IT.
        new_measure = Measure.objects.create(
            name=form.cleaned_data['name'],
            vote_start=form.cleaned_data['vote_start'],
            vote_end=form.cleaned_data['vote_end'],
            summary=form.cleaned_data['summary'],
            quorum=form.cleaned_data['quorum'],
            is_open=form.cleaned_data['is_open'],
        )
        # This view doesn't currently allow banned_accounts to be added. You need
        # use the regular admin for that.
        print "new Measure:", new_measure

        if isinstance(self.request.user.highest_ashmc_role.cast(), DormPresident):
            # Dorm presidents can only send measures to their dorm.
            print "dorm president dorm restriction"
            real_role = self.request.user.highest_ashmc_role.cast()
            dorm_restrictions = [real_role.dorm]
        elif self.request.user.highest_ashmc_role > ASHMCRole.objects.get(title=ASHMCRole.COUNCIL_ROLES[3]):
            print "unrestricted dorm restriction"
            # VP and president can restrict dorm as they choose.
            dorm_restrictions = Dorm.objects.filter(pk__in=self.request.POST.getlist('dorms'))
        else:
            print "no dorm restriction allowed"
            dorm_restrictions = []

        if self.request.user.highest_ashmc_role.title.endswith("Class President"):
            print "Enacting class-president restriction"
            # Year-based class presidents can only send measure to their constituencies.
            if self.request.user.highest_ashmc_role.title.startswith("Senior"):
                year_restrictions = [GradYear.senior_class()]
            elif self.request.user.highest_ashmc_role.title.startswith("Junior"):
                year_restrictions = [GradYear.senior_class() - 1]
            elif self.request.user.highest_ashmc_role.title.startswith("Sophomore"):
                year_restrictions = [GradYear.senior_class() - 2]
            else:
                year_restrictions = [GradYear.senior_class() - 3]
        else:
            year_restrictions = GradYear.objects.filter(pk__in=self.request.POST.getlist('gradyears'))

        print "DR:", dorm_restrictions
        print "YR:", year_restrictions
        # Create the Restrictions object for this new_measure.
        restrictions = new_measure.restrictions
        restrictions.gradyears.add(*[gy for gy in year_restrictions])
        restrictions.dorms.add(*[d for d in dorm_restrictions])

        # Now, ballot creation.
        # Ballots fields all look like r"(\d+)_.*".
        # There will always be at least one ballot; gather all the specified
        # attributes in a ballots_dict, to be used later to create the actual ballots
        # and candidates.
        ballots_dict = {}
        for key in self.request.POST:
            if not re.match(r"^\d+_", key):
                continue

            if self.request.POST[key] == "on":
                keyval = True
            else:
                keyval = self.request.POST[key]

            ballot_sep_index = key.index('_')
            ballot_num = int(key[:ballot_sep_index])
            # create an empty dict with ballot num as key if it doesn't already exist.
            ballots_dict.setdefault(ballot_num, {})
            key_rest = key[ballot_sep_index + 1:]

            if re.match(r"\d+_", key_rest):
                # this is a candidate field -- they will always start with
                # r"\d+_\d+_"
                ballots_dict[ballot_num].setdefault('candidates', {})

                candidate_sep_index = key_rest.index('_')
                candidate_num = int(key_rest[:candidate_sep_index])
                key_rest_rest = key_rest[candidate_sep_index + 1:]
                ballots_dict[ballot_num]['candidates'].setdefault(candidate_num, {})
                ballots_dict[ballot_num]['candidates'][candidate_num][key_rest_rest] = keyval
            else:
                # this is a ballot field.
                ballots_dict[ballot_num][key_rest] = keyval

        print ballots_dict

        for ballot_num in ballots_dict:
            ballot_dict = ballots_dict[ballot_num]
            candidate_info = ballot_dict.pop('candidates', {})

            ballot = Ballot.objects.create(**ballot_dict)
            ballot.measure = new_measure
            ballot.save()
            print ballot
            print "candy keys:", candidate_info.keys()
            for candidate_num in candidate_info.keys():
                candidate_dict = candidate_info[candidate_num]
                is_person = candidate_dict.pop('is_person', False)

                if is_person:
                    print candidate_num, "is a person"
                    usernames = candidate_dict['title'].split(' ')
                    try:
                        # Only support username
                        students = Student.objects.filter(user__username__in=usernames)

                        # if there's any problems, give up and create a normal
                        # candidate.
                        if not students:
                            logger.warn("Could not find usernames: {}".format(usernames))
                            raise Student.DoesNotExist("Could not find usernames: {}".format(usernames))
                        elif len(students) != len(usernames):
                            logger.warn("Could not find all usernames: {} (found {})".format(usernames, students))
                            raise Student.DoesNotExist(
                                "Could not find all usernames: {} (found {})".format(usernames, students)
                            )

                        logger.info("M%dB%d: creating person candidate for %s", new_measure.id, ballot.id, students)
                        candidate = PersonCandidate.objects.create(
                            ballot=ballot,
                            **candidate_dict
                        )
                        for student in students:
                            CandidateUser.objects.create(
                                user=student.user,
                                person_candidate=candidate,
                            )

                    except Student.DoesNotExist:
                        logger.warn("M%dB%d: creating fallback candidate (instead of person candidate)", new_measure.id, ballot.id)
                        candidate = Candidate.objects.create(
                            ballot=ballot,
                            **candidate_dict
                        )
                        print 'new candy keys:', candidate_info.keys()

                else:
                    candidate = Candidate.objects.create(
                        ballot=ballot,
                        **candidate_dict
                    )

        return redirect('vote_measure_results')


class MeasureListing(ListView):
    template_name = "vote/measure_list.html"
    paginate_by = 10

    def get_queryset(self):
        # Put the next-to-expire measures up front.

        this_sem = Semester.get_this_semester()

        if self.request.user.is_superuser:
            return Measure.objects.exclude(vote_end__lte=datetime.datetime.now(pytz.utc)).order_by('vote_end')

        try:
            room = UserRoom.objects.filter(
                user=self.request.user,
                semesters__id=this_sem.id,
                room__dorm__official_dorm=True,
            )[0].room
        except IndexError:
            # If they don't have a room, they're probably not eligible to vote.
            #raise PermissionDenied()
            logger.info("blocked access to {}".format(self.request.user))
            # Until we have roster data importing, this is bad
            pass

        return Measure.objects.exclude(
            # Immediately filter out expired measures. Otherwise shit gets weird.
            vote_end__lte=datetime.datetime.now(pytz.utc),
        ).filter(
            # Hide measures that the user has already voted in.
            ~Q(id__in=Vote.objects.filter(account=self.request.user).values_list('measure__id', flat=True)),
            Q(restrictions__dorms=room.dorm) | Q(restrictions__dorms=None),
            Q(restrictions__gradyears=self.request.user.student.class_of) | Q(restrictions__gradyears=None),
            is_open=True,
            # Only show measures which have already opened for voting
            vote_start__lte=datetime.datetime.now(pytz.utc),
        ).exclude(
            banned_accounts__id__exact=self.request.user.id,
        ).order_by('vote_end')

    def get_context_data(self, *args, **kwargs):
        context = super(MeasureListing, self).get_context_data(*args, **kwargs)

        # Get the last measure the user voted on, and clear it.
        last_measure_voted_id = self.request.session.pop('VOTE_LAST_MEASURE_ID', None)
        if last_measure_voted_id:
            context['last_measure_voted'] = Measure.objects.get(pk=last_measure_voted_id)

        return context


class MeasureDetail(DetailView):
    model = Measure

    def get_object(self):
        object = super(MeasureDetail, self).get_object()
        if self.request.user.is_superuser:
            return object

        if self.request.user in object.banned_accounts.all():
            raise PermissionDenied()

        # make sure it's a vote-able object.
        if not object.is_open or (object.vote_end is not None and object.vote_end < datetime.datetime.now(pytz.utc)):
            logger.info('{} attempted vote on closed measure {}'.format(self.request.user, object))
            raise Http404

        if self.request.user not in object.eligible_voters:
            logger.info("{} attempted to vote in disallowed measure {}".format(self.request.user, object))
            raise Http404

        if Vote.objects.filter(account=self.request.user, measure=object).count() != 0:
            raise Http404

        return object

    def get_context_data(self, *args, **kwargs):
        context = super(MeasureDetail, self).get_context_data(*args, **kwargs)

        if not hasattr(self, 'bad_forms'):
            self.bad_forms = {}
        context['form_errors'] = self.bad_forms

        context['forms'] = [BallotForm(b, data=self.request.POST) for b in self.get_object().ballot_set.all().order_by('display_position')]
        context['VOTE_TYPES'] = Ballot.VOTE_TYPES

        return context

    def post(self, *args, **kwargs):
        if self.request.user.is_superuser:
            return redirect('measure_list')
        measure = self.get_object()

        forms = []
        for ballot in measure.ballot_set.all():
            forms.append(BallotForm(ballot, data=self.request.POST, prefix="{}".format(ballot.id)))

        # Make them vote again if they fucked up
        self.bad_forms = {}
        for f in forms:
            if not f.is_valid():
                self.bad_forms[f.ballot.id] = f.errors

        if self.bad_forms:
            logger.info("bad forms on {} from {}".format(
                    [f.ballot.id for f in forms],
                    self.request.user
                )
            )
            return self.get(*args, **kwargs)

        # all forms valid - safe to create the vote.
        vote = Vote.objects.create(
            measure=measure,
            account=self.request.user,
        )

        for form in forms:
            if form.ballot.vote_type == Ballot.VOTE_TYPES.POPULARITY:
                if form.cleaned_data['choice'] is None:
                    # Valid form with None choice means write in or abstain
                    if not (form.cleaned_data['write_in_value'] or '').strip():
                        # An "empty" (e.g., whitespace-only) write-in is caught
                        # during form validation, so if we get here that means
                        # they're really abstaining.
                        continue

                    c, _ = Candidate.objects.get_or_create(
                        title=form.cleaned_data['write_in_value'],
                        ballot=form.ballot,
                        is_write_in=True,
                    )
                    PopularityVote.objects.create(
                        ballot=form.ballot,
                        vote=vote,
                        candidate=c,
                    )
                else:
                    PopularityVote.objects.create(
                        ballot=form.ballot,
                        vote=vote,
                        candidate=form.cleaned_data['choice'],
                    )

            elif form.ballot.vote_type == Ballot.VOTE_TYPES.SELECT_X:
                for candidate in form.cleaned_data['choice']:
                    PopularityVote.objects.create(
                        ballot=form.ballot,
                        vote=vote,
                        candidate=candidate,
                    )

            elif form.ballot.vote_type == Ballot.VOTE_TYPES.PREFERENCE:
                for candidate_field in form.cleaned_data:
                    candidate = Candidate.objects.get(
                        title=candidate_field,
                        ballot=form.ballot,
                    )
                    PreferentialVote.objects.create(
                        ballot=form.ballot,
                        vote=vote,
                        candidate=candidate,
                        amount=form.cleaned_data[candidate_field],
                    )

            elif form.ballot.vote_type == Ballot.VOTE_TYPES.INOROUT:
                # Don't create a popularityvote if their choice is 'abstain'
                if form.cleaned_data['choice']:
                    PopularityVote.objects.create(
                        ballot=form.ballot,
                        vote=vote,
                        candidate=form.cleaned_data['choice']
                    )

        # Store the voted-on measure for confirmation
        self.request.session['VOTE_LAST_MEASURE_ID'] = measure.id
        logger.info('new vote {} from {}'.format(vote.id, self.request.user))
        return redirect('measure_list')


class MeasureResultList(ListView):
    model = Measure
    template_name = 'vote/measure_results.html'
    paginate_by = 10

    def get_queryset(self):
        if self.request.user.is_superuser:
            return Measure.objects.all().order_by('-vote_end')

        this_sem = Semester.get_this_semester()
        try:
            room = UserRoom.objects.filter(
                user=self.request.user,
                semesters__id=this_sem.id,
                room__dorm__official_dorm=True,
            )[0].room
        except IndexError:
            # If they don't have a room, they're probably not eligible to vote.
            raise PermissionDenied()

        if self.request.user.highest_ashmc_role >= ASHMCRole.objects.get(title="Vice-President"):
            # VP's and higher can see all measures, except those that are dorm-specific
            # because ASHMC has no business within a dorm.
            return Measure.objects.filter(
                Q(restrictions__dorms=room.dorm) | Q(restrictions__dorms=None),
                Q(vote_end__lte=datetime.datetime.now(pytz.utc)) | Q(is_open=False),
            ).order_by('-vote_end')

        return Measure.objects.filter(
            Q(restrictions__dorms=room.dorm) | Q(restrictions__dorms=None),
            Q(restrictions__gradyears=self.request.user.student.class_of) | Q(restrictions__gradyears=None),
            # Only show measures which have already closed for voting
            vote_end__lte=datetime.datetime.now(pytz.utc),
        ).exclude(
            banned_accounts__id__exact=self.request.user.id,
        ).order_by('-vote_end')

