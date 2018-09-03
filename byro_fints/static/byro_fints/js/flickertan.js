$(function(){
    var Flicker = function(fdiv) {
        var interval = 25;
        var data = $(fdiv).data('flicker-content');
        var main = $(fdiv).find('.flicker-main');
        var base_font_size = parseFloat( $(fdiv).css('font-size') );

        var outstream = ['1', '0', '31', '30', '31', '30'];
        for(var i=0; i<data.length; i++) {
            d = parseInt( data.charAt( i ^ 1 ), 16);
            outstream.push(1 | (d << 1) );
            outstream.push(d << 1);
        }
        var index = 0;
        var running = false;

        var update = function() {
            if(!running) {
                return;
            }

            var output = outstream[index];
            index++;
            if(index >= outstream.length) {
                index = 0;
            }
            main.attr('class', 'flicker-main flicker-data-'+output);
            window.setTimeout(update, interval);
        };

        var start = function() {
            $(fdiv).removeClass('flicker-animate-css');
            $(fdiv).addClass('flicker-animate-js');
            running = true;
            $(fdiv).find('.flicker-control-playpause').removeClass('fa-play').addClass('fa-pause');
            update();
        };

        var stop = function() {
            running = false;
            $(fdiv).find('.flicker-control-playpause').removeClass('fa-pause').addClass('fa-play');
        };

        $(fdiv).find('.flicker-control-speedminus').click(function(){
            if(interval < 400) {
                interval = (1000 / ((1000 / interval)-2.5));
            };
        });

        $(fdiv).find('.flicker-control-speedplus').click(function(){
            if(interval > 10) {
                interval = (1000 / ((1000 / interval)+2.5));
            };
        });

        $(fdiv).find('.flicker-control-zoomplus').click(function(){
            $(fdiv).css('font-size',
                parseFloat( $(fdiv).css('font-size') ) + (0.025*base_font_size)
            );
        });

        $(fdiv).find('.flicker-control-zoomminus').click(function(){
            $(fdiv).css('font-size',
                Math.max( parseFloat( $(fdiv).css('font-size') ) - (0.025*base_font_size), 0.01*base_font_size)
            );
        });

        $(fdiv).find('.flicker-control-playpause').click(function(){
            if(running) {
                stop();
            } else {
                start();
            }
        });

        start();
    };
    $('.flicker-code').each(function(){Flicker(this)});
});
