var _paq = window._paq || [];
/* tracker methods like "setCustomDimension" should be called before "trackPageView" */
_paq.push(['disableCookies']);
_paq.push(['trackPageView']);
_paq.push(['enableLinkTracking']);
_paq.push(['setTrackerUrl', 'https://stats.cihar.com/matomo.php']);
_paq.push(['setSiteId', '12']);

$(function () {
    $('.delay-css').attr("rel", "stylesheet");
	$('.menu-show').click(function(e) {
		var $body = $('body');
		$body.toggleClass('open-mobile');
        $('.mobile-menu').toggle();
	});
	$('.open-langs').click(function(e) {
		if($(this).parent().hasClass('opened')){
			$(this).next().fadeOut(300);
			$(this).parent().removeClass('opened');
		} else {
			$(this).next().fadeIn(300);
			$(this).parent().addClass('opened');
		}
		return false;
	});

	$('ul.pricing-tabs li').click(function(){
		var tab_id = $(this).attr('data-tab');

		$('ul.pricing-tabs li').removeClass('current');
		$('.tab-content').removeClass('current');

		$(this).addClass('current');
		$("#"+tab_id).addClass('current');
	});

	$('.pricing-table-tabs-menu ul li').click(function(){
		var tab_id = $(this).attr('data-tab');

		$('.pricing-table-tabs-menu ul li').removeClass('current');
		$('.tab-content').removeClass('current');

		$(this).addClass('current');
		$("#"+tab_id).addClass('current');
	});

    $('.rewards .choose').click(function() {
        var container = $($(this).parents('.reward')[0]);
        container.parent().find('.reward').removeClass('checked');
        container.addClass('checked');
        container.find('input').prop('checked', true);
        return false;
    });
    $('.rewards .close').click(function() {
        $('.rewards .fourth .choose').click();
        return false;
    });
    $('#donate-amount').change(function() {
        var amount = $(this).val();
        var found = 0;
        var highest = $('.reward.fourth');
        $('.reward').each(function () {
            var $this = $(this);
            if ($this.data('amount') <= amount) {
                if ($this.hasClass('small')) {
                    $this.removeClass('small');
                }
                found++;
                if (highest.data('amount') < $this.data('amount')) {
                    highest = $this;
                }
            } else {
                $this.addClass('small');
                if ($this.hasClass('checked')) {
                    $this.find('.close').click();
                }
            }
        });
        highest.find('.choose').click();
        if (found > 1) {
            $('.whoa').show();
            $('.nowhoa').hide();
        } else {
            $('.whoa').hide();
            $('.nowhoa').show();
        }
    });
    $('#donate-amount').change();
    $('#id_vat_0').on('change', function() {
        var value = $(this).val();
        if (value != '') {
            var country = $('#id_country option[value="' + value + '"]');
            country.prop('selected', true);
        }
    });
    $('#id_vat_0,#id_vat_1').on('focusout', function() {
        var country = $('#id_vat_0').val();
        var code = $('#id_vat_1').val();
        if (country && code) {
            var payload = {
                vat: country + code,
                payment: $('input[name="payment"]').val(),
                csrfmiddlewaretoken: $('input[name="csrfmiddlewaretoken"]').val(),
            };
            $.post('/js/vat/', payload , function(data) {
                if (data.valid) {
                    $('input[name="name"]').val(data.name);
                    var parts = data.address.trim().split("\n");
                    $('input[name="address"]').val(parts[0]);
                    $('input[name="city"]').val(parts[parts.length - 1]);
                }
            });
        }
    });
    new ClipboardJS('[data-clipboard-text]');
});
