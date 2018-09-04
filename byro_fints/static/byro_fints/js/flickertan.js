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

        var store_settings = function() {
            if(typeof(Storage) === "undefined") {
                return;
            }
            var data = interval + "__" + $(fdiv).css('font-size');
            localStorage.setItem("flickercode-prefs_default", data);
            localStorage.setItem("flickercode-prefs_"+screen.width, data);
        };

        var load_settings = function() {
            if(typeof(Storage) === "undefined") {
                return;
            }

            var data = localStorage.getItem('flickercode-prefs_'+screen.width);
            if(data === null) {
                data = localStorage.getItem('flickercode-prefs_default');
            }
            if(data === null) {
                return;
            }

            var prefs = data.split("__");
            interval = parseFloat(prefs[0]);
            $(fdiv).css('font-size', prefs[1]);
        };

        var control_done = false;
        var control_clicked = false;

        var control = function(op) {
            if(op == '<') {
                if(interval < 400) {
                    interval = (1000 / ((1000 / interval)-2.5));
                };
            } else if(op == '>') {
                if(interval > 10) {
                    interval = (1000 / ((1000 / interval)+2.5));
                };
            } else if(op == '-') {
                $(fdiv).css('font-size',
                    Math.max( parseFloat( $(fdiv).css('font-size') ) - (0.025*base_font_size), 0.01*base_font_size)
                );
            } else if(op == '+') {
                $(fdiv).css('font-size',
                    parseFloat( $(fdiv).css('font-size') ) + (0.025*base_font_size)
                );
            }
            store_settings();
        };

        var control_down = function(op) {
            control_done = false;
            control_interval = window.setInterval(function(){
                control_done = true;
                control(op)
            }, 150);
        };

        var control_up = function(op) {
            window.clearInterval(control_interval);
        };

        var control_click = function(op) {
            if(!control_done) {
                control(op);
            };
            control_done = false;
        }

        $(fdiv).find('.flicker-control-speedminus').click(function(){
            control_click('<');
        }).mousedown(function(){
            control_down('<');
        }).mouseup(function(){
            control_up('<');
        });

        $(fdiv).find('.flicker-control-speedplus').click(function(){
            control_click('>');
        }).mousedown(function(){
            control_down('>');
        }).mouseup(function(){
            control_up('>');
        });

        $(fdiv).find('.flicker-control-zoomplus').click(function(){
            control_click('+');
        }).mousedown(function(){
            control_down('+');
        }).mouseup(function(){
            control_up('+');
        });

        $(fdiv).find('.flicker-control-zoomminus').click(function(){
            control_click('-');
        }).mousedown(function(){
            control_down('-');
        }).mouseup(function(){
            control_up('-');
        });

        $(fdiv).find('.flicker-control-playpause').click(function(){
            if(running) {
                stop();
            } else {
                start();
            }
        });

        load_settings();
        start();
    };
    $('.flicker-code').each(function(){Flicker(this)});
});
