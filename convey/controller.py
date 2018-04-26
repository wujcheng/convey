import argparse
import csv
import sys
from heapq import nsmallest
from sys import exit

from dialog import Dialog

from .config import Config
from .contacts import Contacts, Attachment
from .dialogue import Cancelled, Debugged, Dialogue, Menu
from .mailSender import MailSenderOtrs, MailSenderSmtp
from .sourcePicker import SourcePicker
from .sourceWrapper import SourceWrapper


class Controller:

    def __init__(self):
        parser = argparse.ArgumentParser(description=__doc__)
        parser.add_argument('file', nargs='?')
        parser.add_argument('--debug', help="On error, enter an ipdb session", default=False, action="store_true")
        parser.add_argument('--fresh', help="Do not attempt to load any previous settings / results", default=False,
                            action="store_true")
        flags = [("otrs_id", "Ticket id"), ("otrs_num", "Ticket num"), ("otrs_cookie", "OTRS cookie"),
                 ("otrs_token", "OTRS token")]
        for flag in flags:
            parser.add_argument('--' + flag[0], help=flag[1])
        parser.add_argument('--csirt-incident', action="store_true",
                            help="Macro that lets you split CSV by fetched incident-contact (whois abuse mail for local country or csirt contact for foreign countries) and send everything by OTRS. You set local countries in config.ini, currently set to: {}".format(
                                Config.get("local_country")))
        args = parser.parse_args()
        if args.debug:
            Config.set("debug", True)

        Config.init()
        Contacts.init()
        file = SourcePicker()  # source file path
        self.wrapper = SourceWrapper(file, args.fresh)
        csv = self.csv = self.wrapper.csv

        # load flags
        for flag in flags:
            if args.__dict__[flag[0]]:
                csv.__dict__[flag[0]] = args.__dict__[flag[0]]
                print("{}: {}".format(flag[1], flag[0]))

        # self.csv.settings = defaultdict(list) # every program launch, the settings resets
        if args.csirt_incident and not csv.is_analyzed:
            csv.settings["split"] = self.extend_column("incident-contact", add=False)
            csv.is_processable = True
            self.process()

        # main menu
        while True:
            csv = self.csv = self.wrapper.csv  # may be changed by reprocessing
            csv.informer.sout_info()

            menu = Menu(title="Main menu - how the file should be processed?")
            menu.add("Pick or delete columns", self.chooseCols)
            menu.add("Add a column", self.addColumn)
            menu.add("Unique filter", self.addUniquing)
            menu.add("Value filter", self.addFiltering)
            menu.add("Split by a column", self.addSplitting)
            menu.add("Change CSV dialect", self.addDialect)
            if self.csv.is_processable:
                menu.add("process", self.process, key="p")
            else:
                menu.add("process  (choose some actions)")
            if csv.is_analyzed and csv.is_split:
                menu.add("send", self.send_menu, key="s")
            else:
                menu.add("send (split first)")
            if csv.is_analyzed:
                menu.add("show all details", lambda: (csv.informer.sout_info(full=True), input()), key="d")
            else:
                menu.add("show all details (process first)")
            menu.add("Refresh...", self.refresh_menu, key="r")
            menu.add("exit", self.close, key="x")

            try:
                menu.sout()
            except Cancelled as e:
                print(e)
                pass
            except Debugged as e:
                import ipdb;
                ipdb.set_trace()

    def send_menu(self):
        if Config.get("otrs_enabled", "OTRS"):
            menu = Menu(title="What sending method do we want to use?", callbacks=False, fullscreen=True)
            menu.add("Send by SMTP...")
            menu.add("Send by OTRS...")
            o = menu.sout()
            if o == '1':
                method = "smtp"
            elif o == '2':
                method = "otrs"
            else:
                print("Unknown option")
                return
        else:
            method = "smtp"

        if method == "otrs":
            sender = MailSenderOtrs(self.csv)
            sender.assure_tokens()
            self.wrapper.save()
        else:
            sender = MailSenderSmtp(self.csv)

        # abuse_count = Contacts.count_mails(self.attachments.keys(), abusemails_only=True)
        # partner_count = Contacts.count_mails(self.attachments.keys(), partners_only=True)
        # info = ["In the next step, we connect to server to send e-mails: {}× to abuse contacts and {}× to partners.".format(abuse_count, partner_count)]
        info = ["In the next step, we connect to the server to send e-mails:"]
        cond1 = cond2 = False
        st = self.csv.stats
        if st["abuse_count"][0]:  # XX should be equal if just splitted by computed column! = self.csv.stats["ispCzFound"]:
            info.append(" Template of a basic e-mail starts: {}".format(Contacts.mailDraft["local"].getMailPreview()))
            cond1 = True
        else:
            info.append(" No non-partner e-mail in the set.")
        if st["partner_count"][0]:  # self.csv.stats["countriesFound"]:
            info.append(" Template of a partner e-mail starts: {}".format(Contacts.mailDraft["foreign"].getMailPreview()))
            cond2 = True
        else:
            info.append(" No partner e-mail in the set.")

        info.append("Do you really want to send e-mails now?")
        if Config.isTesting():
            info.append("\n\n\n*** TESTING MOD - mails will be sent to the address: {} ***"
                            "\n (For turning off testing mode set `testing = False` in config.ini.)".format(Config.get('testing_mail')))
        menu = Menu("\n".join(info), callbacks=False, fullscreen=True)
        if cond1 and cond2:
            menu.add("Send both partner and other e-mails ({}×)".format(st["abuse_count"][0] + st["partner_count"][0]), key="both")
        if cond2:
            menu.add("Send partner e-mails ({}×)".format(st["partner_count"][0]), key="partner")
        if cond1:
            menu.add("Send non-partner e-mails ({}×)".format(st["abuse_count"][0]), key="basic")

        option = menu.sout()

        self.csv.informer.sout_info()  # clear screen
        print("\n\n\n")

        if option is None:
            return

        # XX Terms are equal: abuse == local == other == basic  What should be the universal noun? Let's invent a single word :(
        if option == "both" or option == "basic":
            print("Sending basic e-mails...")
            if not sender.send_list(Attachment.get_basic(self.csv.attachments), Contacts.mailDraft["local"], method=method):
                print("Couldn't send all abuse mails. (Details in mailSender.log.)")
        if option == "both" or option == "partner":
            print("Sending to partner mails...")
            """ Xif not sender.send_list(Contacts.getContacts(self.csv.stats["countriesFound"], partners_only=True),
                                    Contacts.mailDraft["foreign"],
                                    len(self.csv.stats["countriesFound"]),
                                    method=method):"""
            if not sender.send_list(Attachment.get_partner(self.csv.attachments), Contacts.mailDraft["foreign"], method=method):
                print("Couldn't send all partner mails. (Details in mailSender.log.)")

        input("\n\nPress enter to continue...")

    def process(self):
        self.csv.run_analysis()
        self.wrapper.save()


    def refresh_menu(self):
        menu = Menu(title="What should be reprocessed?", fullscreen=True)
        menu.add("Rework whole file again", self.wrapper.clear)
        menu.add("Delete processing settings", self.csv.reset_settings)
        menu.add("Delete whois cache", self.csv.reset_whois)
        menu.add("Resolve unknown abuse-mails", self.csv.resolve_unknown)
        menu.add("Resolve invalid lines", self.csv.resolve_invalid)
        menu.add("Edit mail texts",
                 lambda: Contacts.mailDraft["local"].guiEdit() and Contacts.mailDraft["foreign"].guiEdit())
        menu.sout()

    def extend_column(self, new_field, add=None):
        """ We know what is new column, now determine how we should extend it
                add - bool if the column should be added to the table; None ask
        """
        print("\nWhat column we base {} on?".format(new_field))
        g = self.csv.guesses.get_graph()
        valid_types = list(g.dijkstra(new_field))
        possible_cols = {}
        for (i, key), types in self.csv.guesses.field_type.items():
            for val in types:
                if val in valid_types:
                    possible_cols[i] = types[val]
                    break
        source_col_i = Dialogue.pickOption(self.csv.get_fields_autodetection(),
                                           title="Searching source for " + new_field,
                                           guesses=sorted(possible_cols, key=possible_cols.get, reverse=True))

        dialog = Dialog(autowidgetsize=True)
        method = self.csv.guesses.get_best_method(source_col_i, new_field)
        if method is None:
            # ask how should be treated the column as, even it seems not valid
            # list all known methods to compute the desired new_field (e.g. for incident-contact it is: ip, hostname, ...)
            choices = [(k, self.csv.guesses.get_description(k)) for k, _ in
                       sorted(self.csv.guesses.get_graph().dijkstra(new_field, ignore_private=True).items(),
                              key=lambda v: v[1], reverse=True)]  # whois is an internal keyword

            if len(choices) == 1 and choices[0][0] == "plaintext":
                # if the only method is derivable from a plaintext, no worries that a method
                # converting the column type to "plaintext" is not defined; everything's plaintext
                method = "plaintext"
            elif choices:
                title = "Choose the right method\n\nNo known method for making {} from column {} because the column type wasn't identified. How should I treat the column?".format(
                    new_field, self.csv.fields[source_col_i])
                button, method = dialog.menu(title, choices=choices)
                if button == "cancel":
                    return
            else:
                dialog.msgbox("No known method for making {}. Raise your usecase as an issue at {}.".format(new_field,
                                                                                                            Config.PROJECT_SITE))

        self.csv.settings["add"].append((new_field, source_col_i, method))
        self.csv.fields.append(new_field)

        if add is None:
            if dialog.yesno("New field added: {}\n\nDo you want to include this field as a new column?".format(
                    new_field)) == "ok":
                add = True

        if add is True:  # or (add is None and input("Do you want to include this field as a new column? [y]/n ") not in ["n","no"]):
            self.csv.settings["chosen_cols"].append(len(self.csv.fields) - 1)
        # if add is False or (add is None and input("Do you want to include this field as a new column? [y]/n ") in ["n","no"]):
        # self.csv.settings["chosen_cols"].append(False)
        return len(self.csv.fields) - 1  # + len(self.csv.settings["add"]) - 1

    def chooseCols(self):
        # XX possibility un/check all
        chosens = [(str(i + 1), f, i in self.csv.settings["chosen_cols"]) for i, f in enumerate(self.csv.fields)]
        d = Dialog(autowidgetsize=True)
        ret, values = d.checklist("What fields should be included in the output file?",
                                  choices=chosens)
        if ret == "ok":
            self.csv.settings["chosen_cols"] = [int(v) - 1 for v in values]
            self.csv.is_processable = True

    def selectCol(self, colName="", only_extendables=False, add=None):
        fields = self.csv.get_fields_autodetection() if not only_extendables else []
        for f in self.csv.guesses.extendable_fields:
            d = self.csv.guesses.get_graph().dijkstra(f, ignore_private=True)
            # print(d)
            # import ipdb; ipdb.set_trace()
            s = "from " + ", ".join(sorted([k for k in nsmallest(3, d, key=d.get)]))
            if len(d) > 3:
                s += "..."
            fields.append(("new " + f + "...", s))
        col_i = Dialogue.pickOption(fields, colName)
        if only_extendables or col_i >= len(self.csv.fields):
            newFieldI = col_i if only_extendables else col_i - len(self.csv.fields)
            col_i = self.extend_column(self.csv.guesses.extendable_fields[newFieldI], add=add)
        return col_i

    def addFiltering(self):
        colI = self.selectCol("filter")
        val = Dialogue.ask("What value should the field have to keep the line?")
        self.csv.settings["filter"].append((colI, val))
        self.csv.is_processable = True

    def addSplitting(self):
        self.csv.settings["split"] = self.selectCol("splitting")
        self.csv.is_processable = True

    def addDialect(self):
        dialect = type('', (), {})()
        for k, v in self.csv.dialect.__dict__.copy().items():
            setattr(dialect, k, v)
        # XX not ideal and mostly copies SourceParser.__init__ but this is a great start for a use case we haven't found yet
        # There might be a table with all the csv.dialect properties or so.
        while True:
            sys.stdout.write("What is delimiter: ")
            dialect.delimiter = input()
            if len(dialect.delimiter) != 1:
                print("Delimiter must be a 1-character string. Invent one (like ',').")
                continue
            sys.stdout.write("What is quoting char: ")
            dialect.quotechar = input()
            break
        dialect.quoting = csv.QUOTE_NONE if not dialect.quotechar else csv.QUOTE_MINIMAL

        self.csv.settings["dialect"] = dialect
        self.csv.is_processable = True

    def addColumn(self):
        colI = self.selectCol("new column", only_extendables=True, add=True)
        # self.extend_column(self.csv.guesses.extendable_fields[colI])
        self.csv.is_processable = True

    def addUniquing(self):
        colI = self.selectCol("unique")
        self.csv.settings["unique"].append(colI)
        self.csv.is_processable = True

    def close(self):
        self.wrapper.save()  # resave cache file
        print("Finished.")
        exit(0)