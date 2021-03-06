#
# Copyright 2016 Red Hat, Inc.
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA 02110-1301 USA
#
# Refer to the README and COPYING files for full details of the license
#
#

utilsfile=checkips_utils.py
servicefile=checkipsd
unitfile=vdsm-checkips.service

include $(top_srcdir)/build-aux/Makefile.subs

nodist_noinst_DATA = \
	vdsm-checkips.service \
	config.log

CLEANFILES = \
	$(nodist_noinst_DATA)

EXTRA_DIST = \
	after_get_stats.py \
	vdsm-checkips.service.in \
	$(servicefile) \
	$(utilsfile)


install-data-local: install-data-utils install-data-service install-unit-service
	$(MKDIR_P) $(DESTDIR)$(vdsmhooksdir)/after_get_stats
	$(INSTALL_SCRIPT) $(srcdir)/after_get_stats.py \
		$(DESTDIR)$(vdsmhooksdir)/after_get_stats/10_checkips

install-data-utils:
	$(MKDIR_P) $(DESTDIR)$(vdsmhooksdir)/after_get_stats
	$(INSTALL_DATA) $(srcdir)/$(utilsfile) \
		$(DESTDIR)$(vdsmhooksdir)/after_get_stats/$(utilsfile)

install-data-service:
	$(MKDIR_P) $(DESTDIR)$(vdsmhooksdir)
	$(INSTALL_SCRIPT) $(srcdir)/$(servicefile) \
		$(DESTDIR)$(vdsmhooksdir)/$(servicefile)

install-unit-service:
	$(MKDIR_P) $(DESTDIR)$(SYSTEMD_UNIT_DIR)
	$(INSTALL_SCRIPT) -Dm 0644 $(srcdir)/$(unitfile) \
		$(DESTDIR)$(SYSTEMD_UNIT_DIR)/$(unitfile)

uninstall-local: uninstall-data-utils uninstall-data-service uninstall-unit-service
	$(RM) $(DESTDIR)$(vdsmhooksdir)/after_get_stats/10_checkips

uninstall-data-utils:
	$(RM) $(DESTDIR)$(vdsmhooksdir)/after_get_stats/$(utilsfile)

uninstall-data-service:
	$(RM) $(DESTDIR)$(vdsmhooksdir)/$(servicefile)

uninstall-unit-service:
	$(RM) $(DESTDIR)$(SYSTEMD_UNIT_DIR)/$(unitfile)
